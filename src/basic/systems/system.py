__author__ = 'anushabala'


class System(object):
    def __init__(self):
        pass

    def new_session(self, **kwargs):
        raise NotImplementedError


class SystemTypes(object):
    Simple = "simple"
    Heuristic = "heuristic"
    Neural = "neural"
