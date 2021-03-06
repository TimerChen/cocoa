import math
import random
import re
import numpy as np
import torch
from onmt.Utils import use_gpu

from cocoa.model.vocab import Vocabulary
from cocoa.core.entity import is_entity, Entity, CanonicalEntity

from core.event import Event
from .session import Session
from neural.preprocess import markers, Dialogue
from neural.batcher_rl import Batch
import copy
import time

class NeuralSession(Session):
    def __init__(self, agent, kb, env):
        super(NeuralSession, self).__init__(agent)
        self.env = env
        self.kb = kb
        self.builder = env.utterance_builder
        self.generator = env.dialogue_generator
        self.cuda = env.cuda

        self.batcher = self.env.dialogue_batcher
        self.dialogue = Dialogue(agent, kb, None)
        self.dialogue.kb_context_to_int()
        self.kb_context_batch = self.batcher.create_context_batch([self.dialogue], self.batcher.kb_pad)
        self.max_len = 100

        # Tom
        self.tom = False
        self.controller = None
        if hasattr(env, 'usetom') and env.usetom:
            self.tom = True
            self.critic = env.critic
            self.model = env.model

    def set_controller(self, controller):
        self.controller = controller


    # TODO: move this to preprocess?
    def convert_to_int(self):
        # for i, turn in enumerate(self.dialogue.token_turns):
        #     for curr_turns, stage in zip(self.dialogue.turns, ('encoding', 'decoding', 'target')):
        #         if i >= len(curr_turns):
        #             curr_turns.append(self.env.textint_map.text_to_int(turn, stage))
        self.dialogue.lf_to_int()

    def receive(self, event, another_dia=None):
        if event.action in Event.decorative_events:
            return
        # print(event.data)
        # Parse utterance
        utterance = self.env.preprocessor.process_event(event, self.kb)
        # print('utterance is:', utterance)

        # Empty message
        if utterance is None:
            return

        #print 'receive:', utterance
        # self.dialogue.add_utterance(event.agent, utterance)
        # state = event.metadata.copy()
        # state = {'enc_output': event.metadata['enc_output']}

        # utterance_int = self.env.textint_map.text_to_int(utterance)
        # state['action'] = utterance_int[0]
        state = None
        if another_dia is None:
            self.dialogue.add_utterance_with_state(event.agent, utterance, state)
        else:
            another_dia.add_utterance_with_state(event.agent, utterance, state)


    def _has_entity(self, tokens):
        for token in tokens:
            if is_entity(token):
                return True
        return False

    def attach_punct(self, s):
        s = re.sub(r' ([.,!?;])', r'\1', s)
        s = re.sub(r'\.{3,}', r'...', s)
        s = re.sub(r" 's ", r"'s ", s)
        s = re.sub(r" n't ", r"n't ", s)
        return s

    def _tokens_to_event(self, tokens, output_data):
        # if self.agent == 0 :
        #     try:
        #         tokens = [0, 0]
        #         tokens[0] = markers.OFFER
        #         tokens[1] = '$60'
        #     except ValueError:
        #         #return None
        #         pass

        if isinstance(tokens, tuple):
            tokens = list(tokens)
        if isinstance(tokens[0], int):
            tokens[0] = self.env.vocab.to_word(tokens[0])

        if isinstance(tokens[1], float):
            tokens[1] = CanonicalEntity(type='price', value=tokens[1])

        if len(tokens) > 1 and tokens[0] == markers.OFFER and is_entity(tokens[1]):
            try:
                price = self.builder.get_price_number(tokens[1], self.kb)
                return self.offer({'price': price}, metadata=output_data)
            except ValueError:
                # return None
                pass
        elif tokens[0] == markers.OFFER:
            assert False

        tokens = self.builder.entity_to_str(tokens, self.kb)

        if len(tokens) > 0:
            if tokens[0] == markers.ACCEPT:
                return self.accept(metadata=output_data)
            elif tokens[0] == markers.REJECT:
                return self.reject(metadata=output_data)
            elif tokens[0] == markers.QUIT:
                return self.quit(metadata=output_data)

        while len(tokens) > 0 and tokens[-1] == None: tokens = tokens[:-1]
        s = self.attach_punct(' '.join(tokens))
        # print 'send:', s
        return self.message(s, metadata=output_data)

    def get_value(self, all_events):
        all_dia = []
        # print('-'*5+'get_value:')

        # print('in get value:')
        # last_time = time.time()
        for e in all_events:
            # if info['policy'][act[0]].item() < 1e-7:
            #     continue
            d = copy.deepcopy(self.dialogue)
            self.receive(e, another_dia=d)
            d.lf_to_int()
            # print('='*5)
            # for i, s in enumerate(d.lf_tokens):
            #     print('\t[{}] {}\t{}'.format(d.agents[i], s, d.lfs[i]))
            all_dia.append(d)
        # print('copy all dialogue: ', time.time() - last_time)
        # last_time = time.time()
        batch = self._create_batch(other_dia=all_dia)
        # print('create batch: ', time.time() - last_time)

        # get values
        # batch.mask_last_price()
        e_intent, e_price, e_pmask = batch.encoder_intent, batch.encoder_price, batch.encoder_pmask
        # print('e_intent {}\ne_price{}\ne_pmask{}'.format(e_intent, e_price, e_pmask))
        values = self.critic(e_intent, e_price, e_pmask, batch.encoder_dianum)
        return values


    def send(self, temperature=1, is_fake=False):

        last_time = time.time()

        tokens, output_data = self.generate(is_fake=is_fake, temperature=temperature)

        # if self.tom:
        #     print('generate costs {} time.'.format(time.time() - last_time))
        if is_fake:
            tmp_time = time.time()
            # For the step of choosing U3
            p_mean = output_data['price_mean']
            p_logstd = output_data['price_logstd']
            # get all
            num_price = 5
            all_actions = self.generator._get_all_actions(p_mean, p_logstd, num_price, no_sample=True)
            all_events = []
            new_all_actions = []

            for act in all_actions:
                if output_data['policy'][0, act[0]].item() < 1e-7:
                    continue
                e = self._tokens_to_event(act[:2], output_data)
                all_events.append(e)
                new_all_actions.append(act)
            all_actions = new_all_actions

            print_list = []

            # Get value functions from other one.
            values = self.controller.get_value(self.agent, all_events)

            probs = torch.ones_like(values, device=values.device)
            for i, act in enumerate(all_actions):
                # print('act: ',i ,output_data['policy'], act, probs.shape)
                if act[1] is not None:
                    probs[i, 0] = output_data['policy'][0, act[0]].item() * act[2]
                else:
                    probs[i, 0] = output_data['policy'][0, act[0]].item()

                print_list.append((self.env.textint_map.int_to_text([act[0]]), act, probs[i, 0].item(), values[i, 0].item()))

            # if self.dialogue.lf_tokens[-1]['intent'] == 'offer':
            #     print('-' * 5 + 'u3 debug info: ', len(self.dialogue.lf_tokens))
            #     for i, s in enumerate(self.dialogue.lf_tokens):
            #         print('\t[{}] {} {}\t'.format(self.dialogue.agents[i], s, self.dialogue.lfs[i]))
            #     for s in print_list:
            #         print('\t' + str(s))
            # print('is fake: ',time.time()-tmp_time)

            info = {'values': values, 'probs': probs}
            # print('sum of probs', probs.sum())
            # info['values'] = values
            return (values.mul(probs)).sum()

        last_time=time.time()
        if self.tom:
            # For the step of choosing U2
            # get parameters of normal distribution for price
            p_mean = output_data['price_mean']
            p_logstd = output_data['price_logstd']

            # get all actions
            all_actions = self.generator._get_all_actions(p_mean, p_logstd)
            best_action = (None, None)
            print_list = []

            tom_policy = []
            tom_actions = []

            avg_time = []

            for act in all_actions:
                if output_data['policy'][0, act[0]].item() < 1e-7:
                    continue
                # use fake step to get opponent policy
                tmp_tokens = self._output_to_tokens({'intent': act[0], 'price': act[1]})
                self.dialogue.add_utterance(self.agent, tmp_tokens)
                e = self._tokens_to_event(tmp_tokens, output_data)
                tmp_time = time.time()
                info = self.controller.fake_step(self.agent, e)
                avg_time.append(time.time() - tmp_time)
                self.dialogue.delete_last_utterance(delete_state=False)
                self.controller.step_back(self.agent)

                tmp = info.exp() * output_data['policy'][0, act[0]]
                # choice the best action
                # if best_action[1] is None or tmp.item() > best_action[1]:
                #     best_action = (tmp_tokens, tmp.item())
                # record all the actions
                tom_policy.append(tmp.item())
                tom_actions.append(tmp_tokens)

                print_list.append((self.env.textint_map.int_to_text([act[0]]), act, tmp.item(), info.item(), output_data['policy'][0, act[0]].item()))

            # print('fake_step costs {} time.'.format(np.mean(avg_time)))

            # Sample action from new policy
            final_action = torch.multinomial(torch.from_numpy(np.array(tom_policy),), 1).item()
            tokens = list(tom_actions[final_action])

            # print('-'*5+'tom debug info: ', len(self.dialogue.lf_tokens))
            # for s in print_list:
            #     print('\t'+ str(s))
            # self.dialogue.lf_to_int()
            # for s in self.dialogue.lfs:
            #     print(s)
        # if self.tom:
        #     print('the whole tom staff costs {} times.'.format(time.time() - last_time))

        if tokens is None:
            return None
        self.dialogue.add_utterance(self.agent, list(tokens))
        # print('tokens', tokens)
        # self.dialogue.add_utterance_with_state(self.agent, list(tokens), output_data)
        return self._tokens_to_event(tokens, output_data)


    def step_back(self):
        # Delete utterance from receive
        self.dialogue.delete_last_utterance(delete_state=True)

    def iter_batches(self):
        """Compute the logprob of each generated utterance.
        """
        self.convert_to_int()
        batches = self.batcher.create_batch([self.dialogue], for_value=True)
        # print('number of batches: ', len(batches))
        yield len(batches)
        for batch in batches:
            # TODO: this should be in batcher
            batch = Batch(batch['encoder_args'],
                          batch['decoder_args'],
                          batch['context_data'],
                          self.env.vocab,
                          num_context=Dialogue.num_context, cuda=self.env.cuda,
                          for_value=batch['for_value'])
            yield batch


class PytorchNeuralSession(NeuralSession):
    def __init__(self, agent, kb, env):
        super(PytorchNeuralSession, self).__init__(agent, kb, env)
        self.vocab = env.vocab

        self.new_turn = False
        self.end_turn = False

    def get_decoder_inputs(self):
        # Don't include EOS
        utterance = self.dialogue._insert_markers(self.agent, [], True)[:-1]
        inputs = self.env.textint_map.text_to_int(utterance, 'decoding')
        inputs = np.array(inputs, dtype=np.int32).reshape([1, -1])
        return inputs

    def _create_batch(self, other_dia=None):
        num_context = Dialogue.num_context

        # All turns up to now
        self.convert_to_int()
        if other_dia is None:
            dias = [self.dialogue]
        else:
            dias = other_dia
        encoder_turns = self.batcher._get_turn_batch_at(dias, Dialogue.ENC, -1, step_back=self.batcher.state_length)

        encoder_inputs = self.batcher.get_encoder_inputs(encoder_turns)
        # print('intent in sess: ', encoder_inputs[0])
        # encoder_context = self.batcher.get_encoder_context(encoder_turns, num_context)
        encoder_args = {
                        'intent': encoder_inputs[0],
                        'price': encoder_inputs[1],
                        'price_mask': encoder_inputs[2],
                        # 'context': encoder_context
                    }


        roles = self.batcher._get_turn_batch_at(dias, Dialogue.ROLE, -1)
        if self.batcher.dia_num:
            for a in roles:
                a.append(len(dias[0].lfs) / self.batcher.dia_num)
            encoder_args['dia_num'] = roles
            # encoder_args['dia_num'] = [len(dias[0].lfs) / self.batcher.dia_num] * len(encoder_inputs[0])

        decoder_args = {
                        'intent': encoder_inputs[0],
                        'price': encoder_inputs[1],
                        'price_mask': encoder_inputs[2],
                        'context': self.kb_context_batch,
                    }

        context_data = {
                'agents': [self.agent],
                'kbs': [self.kb],
                }
        # print('[self.vocab]: ', self.vocab)
        return Batch(encoder_args, decoder_args, context_data,
                self.vocab, num_context=num_context, cuda=self.cuda)

    def generate(self, temperature=1, is_fake=False):
        if len(self.dialogue.agents) == 0:
            self.dialogue._add_utterance(1 - self.agent, [], lf={'intent': 'start'})
            # TODO: Need we add an empty state?
        batch = self._create_batch()

        output_data = self.generator.generate_batch(batch, enc_state=None, whole_policy=is_fake, temperature=temperature)

        entity_tokens = self._output_to_tokens(output_data)

        return entity_tokens, output_data

    def _is_valid(self, tokens):
        if not tokens:
            return False
        if Vocabulary.UNK in tokens:
            return False
        return True

    def _output_to_tokens(self, data):
        # print(data['intent'], data['price'])
        not_pytorch = False
        if isinstance(data["intent"], int):
            not_pytorch = True

        if not_pytorch:
            predictions = [data["intent"]]
        else:
            predictions = [data["intent"].item()]
        if data.get('price') is not None:
            if not_pytorch:
                p = data['price']
            else:
                p = data['price'].item()
            p = max(p, 0.0)
            p = min(p, 1.0)
            predictions += [p]
        else:
            predictions += [None]

        tokens = self.builder.build_target_tokens(predictions, self.kb)
        # print('converting to tokens: {} -> {}'.format(predictions, tokens))
        return tokens

