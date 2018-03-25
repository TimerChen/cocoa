import random
import re
from itertools import izip
import numpy as np

from cocoa.model.vocab import Vocabulary
from cocoa.core.entity import is_entity, Entity

from core.event import Event
from session import Session
from model.preprocess import markers, Dialogue
from model.evaluate import EncDecEvaluator

class NeuralSession(Session):
    def __init__(self, agent, kb, env):
        super(NeuralSession, self).__init__(agent)
        self.env = env
        self.model = env.model
        self.kb = kb

        self.batcher = self.env.dialogue_batcher
        self.dialogue = Dialogue(agent, kb, None)
        self.dialogue.kb_context_to_int()
        self.kb_context_batch = self.batcher.create_context_batch([self.dialogue], self.batcher.kb_pad)
        self.max_len = 100

    def convert_to_int(self):
        for i, turn in enumerate(self.dialogue.token_turns):
            curr_turns = self.dialogue.turns[Dialogue.ENC]
            if i >= len(curr_turns):
                curr_turns.append(self.env.textint_map.text_to_int(turn, 'encoding'))
            else:
                # Already converted
                pass

    def receive(self, event):
        if event.action in Event.decorative_events:
            return
        # Parse utterance
        utterance = self.env.preprocessor.process_event(event, self.kb)
        # Empty message
        if utterance is None:
            return

        print 'receive:', utterance
        self.dialogue.add_utterance(event.agent, utterance)

    def _has_entity(self, tokens):
        for token in tokens:
            if is_entity(token):
                return True
        return False

    def attach_punct(self, s):
        s = re.sub(r' ([.,!?;])', r'\1', s)
        s = re.sub(r'\.{3,}', r'...', s)
        return s

    def map_prices(self, entity_tokens):
        # NOTE: entities are CanonicalEntities, change to Entity
        entity_tokens = Dialogue.original_price(self.kb, entity_tokens)
        tokens = [str(x.canonical.value) if is_entity(x) else x for x in entity_tokens]
        return tokens

    def send(self):
        for i in xrange(1):
            tokens = self.generate()
            if tokens is not None:
                break
        if tokens is None:
            return None

        self.dialogue.add_utterance(self.agent, list(tokens))
        tokens = self.map_prices(tokens)

        if len(tokens) > 0:
            if tokens[0] == markers.OFFER:
                try:
                    return self.offer({'price': float(tokens[1])})
                except ValueError:
                    return None
            elif tokens[0] == markers.ACCEPT:
                return self.accept()
            elif tokens[0] == markers.REJECT:
                return self.reject()

        s = self.attach_punct(' '.join(tokens))
        print 'send:', s
        return self.message(s)

class GeneratorNeuralSession(NeuralSession):
    def __init__(self, agent, kb, env):
        super(GeneratorNeuralSession, self).__init__(agent, kb, env)
        self.encoder_state = None
        self.decoder_state = None
        self.encoder_output_dict = None

        self.new_turn = False
        self.end_turn = False

    def _decoder_inputs(self):
        utterance = self.dialogue._insert_markers(self.agent, [], True)
        inputs = self.env.textint_map.text_to_int(utterance, 'decoding')
        inputs = np.array(inputs, dtype=np.int32).reshape([1, -1])
        inputs = inputs[:, :self.model.decoder.prompt_len]
        return inputs

    def _create_batch(self):
        num_context = Dialogue.num_context
        # All turns up to now
        self.convert_to_int()
        encoder_turns = self.batcher._get_turn_batch_at([self.dialogue], Dialogue.ENC, None)
        inputs = self.batcher.get_encoder_inputs(encoder_turns)
        context = self.batcher.get_encoder_context(encoder_turns, num_context)
        encoder_args = {
                'inputs': inputs,
                'context': context,
                }
        decoder_args = {
                'inputs': self._decoder_inputs(),
                'context': self.kb_context_batch,
                }
        batch = {
                'encoder_args': encoder_args,
                'decoder_args': decoder_args,
                }
        return batch

    def _decoder_args(self, entity_tokens):
        inputs = self._process_entity_tokens(entity_tokens, 'decoding')
        decoder_args = {'inputs': inputs,
                'last_inds': self._get_last_inds(inputs),
                'init_state': self.decoder_state,
                'textint_map': self.env.textint_map,
                }
        return decoder_args

    def output_to_tokens(self, output_dict):
        entity_tokens = self._pred_to_token(output_dict['preds'])[0]
        return entity_tokens

    def generate(self):
        sess = self.env.tf_session

        if len(self.dialogue.agents) == 0:
            self.dialogue._add_utterance(1 - self.agent, [])
        batch = self._create_batch()
        encoder_init_state = None

        output_dict = self.model.generate(sess, batch, encoder_init_state, max_len=self.max_len, textint_map=self.env.textint_map)
        entity_tokens = self.output_to_tokens(output_dict)

        print 'generate:', entity_tokens
        if not self._is_valid(entity_tokens):
            return None
        return entity_tokens

    def _is_valid(self, tokens):
        if not tokens:
            return False
        if Vocabulary.UNK in tokens:
            return False
        return True

    def _pred_to_token(self, preds):
        entity_tokens, _ = EncDecEvaluator.pred_to_token(preds, self.env.stop_symbol, self.env.remove_symbols, self.env.textint_map)
        return entity_tokens

class SelectorNeuralSession(GeneratorNeuralSession):
    def _create_batch(self):
        # Add candidates
        candidates = self.env.retriever.search(self.kb.role, self.kb.category, self.kb.title, self.dialogue.token_turns)
        token_candidates = [c['response'] for c in candidates]
        int_candidates = [self.env.textint_map.text_to_int(c, 'decoding') for c in token_candidates]
        self.dialogue.candidates = [int_candidates]
        candidates = self.batcher._get_candidate_batch_at([self.dialogue], 0)

        batch = super(SelectorNeuralSession, self)._create_batch()
        batch['decoder_args']['candidates'] = candidates
        batch['token_candidates'] = [token_candidates]

        return batch

    def output_to_tokens(self, output_dict):
        entity_tokens = []
        for token in output_dict['responses'][0]:
            if token == markers.EOS:
                break
            entity_tokens.append(token)
        # Remove 'prompts', e.g. <go>
        entity_tokens = entity_tokens[2:]
        return entity_tokens

class PyTorchNeuralSession(NeuralSession):
    def __init__(self, agent, kb, env):
        super(GeneratorNeuralSession, self).__init__(agent, kb, env)
        self.encoder_state = None
        self.decoder_state = None
        self.encoder_output_dict = None

        self.new_turn = False
        self.end_turn = False

    def _decoder_inputs(self):
        utterance = self.dialogue._insert_markers(self.agent, [], True)
        inputs = self.env.textint_map.text_to_int(utterance, 'decoding')
        inputs = np.array(inputs, dtype=np.int32).reshape([1, -1])
        inputs = inputs[:, :self.model.decoder.prompt_len]
        return inputs

    def _create_batch(self):
        num_context = Dialogue.num_context
        # All turns up to now
        self.convert_to_int()
        encoder_turns = self.batcher._get_turn_batch_at([self.dialogue], Dialogue.ENC, None)
        inputs = self.batcher.get_encoder_inputs(encoder_turns)
        context = self.batcher.get_encoder_context(encoder_turns, num_context)
        encoder_args = {
                'inputs': inputs,
                'context': context,
                }
        decoder_args = {
                'inputs': self._decoder_inputs(),
                'context': self.kb_context_batch,
                }
        batch = {
                'encoder_args': encoder_args,
                'decoder_args': decoder_args,
                }
        return batch

    def _decoder_args(self, entity_tokens):
        inputs = self._process_entity_tokens(entity_tokens, 'decoding')
        decoder_args = {'inputs': inputs,
                'last_inds': self._get_last_inds(inputs),
                'init_state': self.decoder_state,
                'textint_map': self.env.textint_map,
                }
        return decoder_args

    def output_to_tokens(self, output_dict):
        entity_tokens = self._pred_to_token(output_dict['preds'])[0]
        return entity_tokens

    def generate(self):
        if len(self.dialogue.agents) == 0:
            self.dialogue._add_utterance(1 - self.agent, [])
        batch = self._create_batch()
        encoder_init_state = None

        output_dict = self.model.generate(sess, batch, encoder_init_state, max_len=self.max_len, textint_map=self.env.textint_map)
        entity_tokens = self.output_to_tokens(output_dict)

        print 'generate:', entity_tokens
        if not self._is_valid(entity_tokens):
            return None
        return entity_tokens

    def _is_valid(self, tokens):
        if not tokens:
            return False
        if Vocabulary.UNK in tokens:
            return False
        return True

    def _pred_to_token_original(self, preds):
        entity_tokens, _ = EncDecEvaluator.pred_to_token(preds, self.env.stop_symbol, self.env.remove_symbols, self.env.textint_map)
        return entity_tokens

    def _build_target_tokens(self, pred):
        vocab = self.vocab
        tokens = []
        for tok in pred:
            # str() to convert Entity
            tokens.append(str(vocab.ind_to_word[tok]))
            if tokens[-1] == markers.EOS:
                tokens = tokens[:-1]
                break
        return tokens

    def _pred_to_token_new(self, opt, model_opt, data, split='test'):
        scorer = Scorer(opt.alpha)

        generator = Generator(self.model, self.vocab,
                              beam_size=opt.beam_size,
                              n_best=opt.n_best,
                              max_length=opt.max_length,
                              global_scorer=scorer,
                              cuda=use_gpu(opt),
                              min_length=opt.min_length)

        builder = UtteranceBuilder(self.vocab, opt.n_best, has_tgt=True)

        # Statistics
        counter = count(1)
        pred_score_total, pred_words_total = 0, 0
        gold_score_total, gold_words_total = 0, 0

        out_file = sys.stdout

        data_iter = data.generator(split, shuffle=False)
        num_batches = data_iter.next()
        for batch in data_iter:
            batch_data = generator.generate_batch(batch, gt_prefix=self.gt_prefix)
            utterances = builder.from_batch(batch_data)

            for trans in utterances:
                pred_score_total += trans.pred_scores[0]
                pred_words_total += len(trans.pred_sents[0])
                gold_score_total += trans.gold_score
                gold_words_total += len(trans.gold_sent)

                n_best_preds = [" ".join(pred)
                                for pred in trans.pred_sents[:opt.n_best]]
                out_file.write('\n'.join(n_best_preds))
                out_file.write('\n')
                out_file.flush()

                if opt.verbose:
                    sent_number = next(counter)
                    output = trans.log(sent_number)
                    os.write(1, output.encode('utf-8'))