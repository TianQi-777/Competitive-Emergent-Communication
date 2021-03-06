# class defintions for chatbots - questioner and answerer

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.autograd as autograd
from torch.distributions import Categorical
import sys
from utilities import initializeWeights

import pdb, pickle
#---------------------------------------------------------------------------
# Parent class for both q and a bots
class ChatBot(nn.Module):
    def __init__(self, params):
        super(ChatBot, self).__init__();

        # absorb all parameters to self
        for attr in params: setattr(self, attr, params[attr]);

        # standard initializations
        self.hState = torch.Tensor();
        self.cState = torch.Tensor();
        self.actions = [];
        self.actionLosses = [];
        self.evalFlag = False;

        # modules (common)
        self.inNet = nn.Embedding(self.inVocabSize, self.embedSize);
        self.outNet = nn.Linear(self.hiddenSize, self.outVocabSize);
        self.softmax = nn.Softmax(dim=1);

        # initialize weights
        initializeWeights([self.inNet, self.outNet], 'xavier');

    # initialize hidden states
    def resetStates(self, batchSize, retainActions=False):
        # create tensors
        self.hState = torch.Tensor(batchSize, self.hiddenSize);
        self.hState.fill_(0.0);
        self.hState = Variable(self.hState);
        self.cState = torch.Tensor(batchSize, self.hiddenSize);
        self.cState.fill_(0.0);
        self.cState = Variable(self.cState);

        if self.useGPU:
            self.hState = self.hState.cuda();
            self.cState = self.cState.cuda();

        # new episode
        if not retainActions:
            self.actions = [];
            self.actionLosses = [];

    # freeze agent
    def freeze(self):
        for p in self.parameters(): p.requires_grad = False;
    # unfreeze agent
    def unfreeze(self):
        for p in self.parameters(): p.requires_grad = True;

    # given an input token, interact for the next round
    def listen(self, inputToken, imgEmbed = None):
        # embed and pass through LSTM
        tokenEmbeds = self.inNet(inputToken);
        # concat with image representation
        if imgEmbed is not None:
            tokenEmbeds = torch.cat((tokenEmbeds, imgEmbed), 1);

        # now pass it through rnn
        self.hState, self.cState = self.rnn(tokenEmbeds,\
                                            (self.hState, self.cState));

    # speak a token
    def speak(self):
        # compute softmax and choose a token
        outDistr = self.softmax(self.outNet(self.hState));

        # if evaluating
        if self.evalFlag:
            _, actions = outDistr.max(1);
        else:
            m = Categorical(outDistr);
            actions = m.sample();
            # record actions
            self.actions.append(actions.unsqueeze(1));
            self.actionLosses.append(-m.log_prob(actions).unsqueeze(1));
        return actions;

    # reinforce each state with reward
    def reinforce(self, rewards):
        for i in range(len(self.actionLosses)):
            self.actionLosses[i] *= rewards;

    # backward computation
    def performBackward(self):
        if self.useGPU:
	    tmp = [torch.ones(a.data.shape).cuda() for a in self.actionLosses];
        else:
	    tmp = [torch.ones(a.data.shape) for a in self.actionLosses];
        autograd.backward(self.actionLosses, tmp, retain_graph=True);

    # switch mode to evaluate
    def evaluate(self): self.evalFlag = True;

    # switch mode to train
    def train(self): self.evalFlag = False;

#---------------------------------------------------------------------------
class Answerer(ChatBot):
    def __init__(self, params):
        self.parent = super(Answerer, self);
        # input-output for current bot
        params['inVocabSize'] = params['aInVocab'];
        if params['overhear']:
            params['inVocabSize'] += params['qOutVocab'];
        params['outVocabSize'] = params['aOutVocab'];
        self.parent.__init__(params);

        # number of attribute values
        numAttrs = sum([len(ii) for ii in self.props.values()]);
        # number of unique attributes
        numUniqAttr = len(self.props);

        # rnn inputSize
        rnnInputSize = numUniqAttr * self.imgFeatSize + self.embedSize;

        self.imgNet = nn.Embedding(numAttrs, self.imgFeatSize);
        self.rnn = nn.LSTMCell(rnnInputSize, self.hiddenSize);
        initializeWeights([self.rnn, self.imgNet], 'xavier');

        # set offset
        self.listenOffset = params['qOutVocab'];
        self.noneToken = self.listenOffset + params['aOutVocab'];
        self.overhearOffset = self.noneToken + 1;

    # Embedding the image
    def embedImage(self, batch):
        embeds = self.imgNet(batch);
        # concat instead of add
        features = torch.cat(tuple(embeds.transpose(0, 1)), 1);
        # add features
        #features = torch.sum(embeds, 1).squeeze(1);

        return features;

#---------------------------------------------------------------------------
class Questioner(ChatBot):
    def __init__(self, params):
        self.parent = super(Questioner, self);
        # input-output for current bot
        params['inVocabSize'] = params['qInVocab'];
        if params['overhear']:
            params['inVocabSize'] += params['aOutVocab'];
            if params['overhearTask']:
                params['inVocabSize'] += params['numTasks'];
        params['outVocabSize'] = params['qOutVocab'];
        self.parent.__init__(params);

        # always condition on task
        #self.rnn = nn.LSTMCell(2*self.embedSize, self.hiddenSize);
        self.rnn = nn.LSTMCell(self.embedSize, self.hiddenSize);

        # additional prediction network
        # start token included
        numPreds = sum([len(ii) for ii in self.props.values()]);
        # network for predicting
        self.predictRNN = nn.LSTMCell(self.embedSize, self.hiddenSize);
        self.predictNet = nn.Linear(self.hiddenSize, numPreds);
        initializeWeights([self.predictNet, self.predictRNN, self.rnn], 'xavier');

        # setting offset
        # aOut | qOut | tasks | none | noneTask | aOut | tasks
        self.listenOffset = params['aOutVocab'];
        self.taskOffset = params['aOutVocab'] + params['qOutVocab'];
        self.noneToken = self.taskOffset + params['numTasks'];
        self.noneTaskToken = self.noneToken + 1;
        self.overhearOffset = self.noneTaskToken + 1;
        self.overhearTaskOffset = params['numTasks'] + 2 + params['aOutVocab'];

    # make a guess the given image
    def guessAttribute(self, inputEmbeds):
        # compute softmax and choose a token
        self.hState, self.cState = \
                self.predictRNN(inputEmbeds, (self.hState, self.cState));
        outDistr = self.softmax(self.predictNet(self.hState));

        # if evaluating
        if self.evalFlag:
            _, actions = outDistr.max(1);
            return actions, outDistr;
        else:
            m = Categorical(outDistr);
            actions = m.sample();
            # record actions
            self.actions.append(actions.unsqueeze(1));
            self.actionLosses.append(-m.log_prob(actions).unsqueeze(1));

        return actions.unsqueeze(1), outDistr;

    # returning the answer, from the task
    def predict(self, tasks, numTokens):
        guessTokens = [];
        guessDistr = [];

        for _ in xrange(numTokens):
            # explicit task dependence
            taskEmbeds = self.inNet(tasks);
            guess, distr = self.guessAttribute(taskEmbeds);

            # record the guess and distribution
            guessTokens.append(guess);
            guessDistr.append(distr);

        # return prediction
        return guessTokens, guessDistr;

    # Embedding the image
    def embedTask(self, tasks): return self.inNet(tasks + self.taskOffset);

#---------------------------------------------------------------------------
class Team:
    # initialize
    def __init__(self, params):
        # memorize params
        for field, value in params.iteritems(): setattr(self, field, value);
        self.aBot1 = Answerer(params);
        self.qBot1 = Questioner(params);
        self.aBot2 = Answerer(params);
        self.qBot2 = Questioner(params);
        self.criterion = nn.NLLLoss();
        self.reward1 = torch.Tensor(self.batchSize, 1);
        self.reward2 = torch.Tensor(self.batchSize, 1);
        self.totalReward1 = None;
        self.totalReward2 = None;
        self.rlNegReward = -10*self.rlScale;

        # ship to gpu if needed
        if self.useGPU:
            self.aBot1 = self.aBot1.cuda();
            self.qBot1 = self.qBot1.cuda();
            self.aBot2 = self.aBot2.cuda();
            self.qBot2 = self.qBot2.cuda();
            self.reward1 = self.reward1.cuda();
            self.reward2 = self.reward2.cuda();

        print(self.aBot1)
        print(self.qBot1)
        print(self.aBot2)
        print(self.qBot2)

    # switch to train
    def train(self):
        self.aBot1.train(); self.qBot1.train();
        self.aBot2.train(); self.qBot2.train();

    # switch to evaluate
    def evaluate(self):
        self.aBot1.evaluate(); self.qBot1.evaluate();
        self.aBot2.evaluate(); self.qBot2.evaluate();

    def setOverhear(self, overhear):
        self.overhear = overhear;

    def setOverhearTask(self, overhearTask):
        self.overhearTask = overhearTask;

    # forward pass
    def forward(self, batch1, tasks1, batch2, tasks2, record=False):
        # reset the states of the bots
        batchSize = batch1.size(0);
        self.qBot1.resetStates(batchSize);
        self.aBot1.resetStates(batchSize);
        self.qBot2.resetStates(batchSize);
        self.aBot2.resetStates(batchSize);

        # get image representation
        imgEmbed1 = self.aBot1.embedImage(batch1);
        imgEmbed2 = self.aBot2.embedImage(batch2);

        # ask multiple rounds of questions
        aBot1Reply = tasks1 + self.qBot1.taskOffset;
        aBot2Reply = tasks2 + self.qBot2.taskOffset;
        # if the conversation is to be recorded
        talk1 = [];
        talk2 = [];
        for roundId in xrange(self.numRounds):
            # listen to answer, ask q_r, and listen to q_r as well
            self.qBot1.listen(aBot1Reply);
            self.qBot2.listen(aBot2Reply);
            # overhear the aBot reply from the other team
            # only if not task
            if self.overhear and \
                    (aBot1Reply < self.qBot1.taskOffset).data.all() and \
                    (aBot2Reply < self.qBot2.taskOffset).data.all():
                self.qBot1.listen(self.qBot1.overhearOffset + aBot2Reply);
                self.qBot2.listen(self.qBot2.overhearOffset + aBot1Reply);
            elif self.overhearTask and \
                    (aBot1Reply >= self.qBot1.taskOffset).data.all() and \
                    (aBot2Reply >= self.qBot2.taskOffset).data.all():
                self.qBot1.listen(self.qBot1.overhearTaskOffset + aBot2Reply);
                self.qBot2.listen(self.qBot2.overhearTaskOffset + aBot1Reply);
            else:
                self.qBot1.listen(aBot2Reply.clone().fill_(self.qBot1.noneTaskToken));
                self.qBot2.listen(aBot1Reply.clone().fill_(self.qBot2.noneTaskToken));
            qBot1Ques = self.qBot1.speak();
            qBot2Ques = self.qBot2.speak();

            # clone
            qBot1Ques = qBot1Ques.detach();
            qBot2Ques = qBot2Ques.detach();
            # make this random
            self.qBot1.listen(self.qBot1.listenOffset + qBot1Ques);
            self.qBot2.listen(self.qBot2.listenOffset + qBot2Ques);

            # Aer is memoryless, forget
            if not self.remember:
                self.aBot1.resetStates(batchSize, True);
                self.aBot2.resetStates(batchSize, True);
            # listen to question and answer, also listen to answer
            self.aBot1.listen(qBot1Ques, imgEmbed1);
            self.aBot2.listen(qBot2Ques, imgEmbed2);
            # overhear question from other team
            if self.overhear:
                self.aBot1.listen(self.aBot1.overhearOffset + qBot2Ques, imgEmbed1);
                self.aBot2.listen(self.aBot2.overhearOffset + qBot1Ques, imgEmbed2);
            else:
                self.aBot1.listen(qBot2Ques.clone().fill_(self.aBot1.noneToken),
                    imgEmbed1);
                self.aBot2.listen(qBot1Ques.clone().fill_(self.aBot2.noneToken),
                    imgEmbed2);
            aBot1Reply = self.aBot1.speak();
            aBot1Reply = aBot1Reply.detach();
            aBot2Reply = self.aBot2.speak();
            aBot2Reply = aBot2Reply.detach();
            self.aBot1.listen(aBot1Reply + self.aBot1.listenOffset, imgEmbed1);
            self.aBot2.listen(aBot2Reply + self.aBot2.listenOffset, imgEmbed2);

            if record:
                talk1.extend([qBot1Ques, aBot1Reply]);
                talk2.extend([qBot2Ques, aBot2Reply]);

        # listen to the last answer
        self.qBot1.listen(aBot1Reply);
        self.qBot2.listen(aBot2Reply);
        # overhear last answer from other team
        if self.overhear:
            self.qBot1.listen(self.qBot1.overhearOffset + aBot2Reply);
            self.qBot2.listen(self.qBot2.overhearOffset + aBot1Reply);
        else:
            self.qBot1.listen(aBot2Reply.clone().fill_(self.qBot1.noneToken));
            self.qBot2.listen(aBot1Reply.clone().fill_(self.qBot2.noneToken));

        # predict the image attributes, compute reward
        self.guessToken1, self.guessDistr1 = self.qBot1.predict(tasks1, 2);
        self.guessToken2, self.guessDistr2 = self.qBot2.predict(tasks2, 2);

        return self.guessToken1, self.guessToken2, self.guessDistr1,\
            self.guessDistr2, talk1, talk2;

    # backward pass
    def backward(self, optimizer, gtLabels1, gtLabels2, epoch, baseline=None):
        # compute reward
        self.reward1.fill_(self.rlNegReward);
        self.reward2.fill_(self.rlNegReward);

        # both attributes need to match
        firstMatch1 = self.guessToken1[0].data == gtLabels1[:, 0:1];
        secondMatch1 = self.guessToken1[1].data == gtLabels1[:, 1:2];
        match1 = firstMatch1 & secondMatch1;
        firstMatch2 = self.guessToken2[0].data == gtLabels2[:, 0:1];
        secondMatch2 = self.guessToken2[1].data == gtLabels2[:, 1:2];
        match2 = firstMatch2 & secondMatch2;
        # if overhear, assign reward according to matrix (1 is rlScale):
        #                  | team 2 correct | team 2 incorrect
        # team 1 correct   | (1,1)          | (10,-100)
        # team 1 incorrect | (-100,10)      | (-10,-10)

        # Paul changed this
        # if self.overhear or self.rshare:
        if self.rshare:
            self.reward1[match1 & match2] = self.rlScale;   # +100 each if both right
            self.reward1[match1 & ~match2] = self.rlPosMult * self.rlScale; # +100 if im right, he's wrong
            self.reward1[~match1 & match2] = self.rlNegMult * self.rlNegReward; #-10000 if im wrong, he's right
            self.reward2[match1 & match2] = self.rlScale;   # +100 each if both right
            self.reward2[~match1 & match2] = self.rlPosMult * self.rlScale; # +100 if im right, he's wrong
            self.reward2[match1 & ~match2] = self.rlNegMult * self.rlNegReward; #-10000 if im wrong, he's right
        else:
            self.reward1[match1] = self.rlScale;    # +100 if right and -1000 if wrong
            self.reward2[match2] = self.rlScale;    # +100 if right and -1000 if wrong

        # import pdb; pdb.set_trace()

        # reinforce all actions for qBot, aBot
        self.qBot1.reinforce(self.reward1);
        self.aBot1.reinforce(self.reward1);
        self.qBot2.reinforce(self.reward2);
        self.aBot2.reinforce(self.reward2);

        # optimize
        optimizer.zero_grad();
        self.qBot1.performBackward();
        self.aBot1.performBackward();
        self.qBot2.performBackward();
        self.aBot2.performBackward();

        # clamp the gradients
        for p in self.qBot1.parameters(): p.grad.data.clamp_(min=-5., max=5.);
        for p in self.aBot1.parameters(): p.grad.data.clamp_(min=-5., max=5.);
        for p in self.qBot2.parameters(): p.grad.data.clamp_(min=-5., max=5.);
        for p in self.aBot2.parameters(): p.grad.data.clamp_(min=-5., max=5.);

        # cummulative reward
        batchReward1 = torch.mean(self.reward1)/self.rlScale;
        batchReward2 = torch.mean(self.reward2)/self.rlScale;
        if self.totalReward1 is None: self.totalReward1 = batchReward1;
        if self.totalReward2 is None: self.totalReward2 = batchReward2;
        self.totalReward1 = 0.95 * self.totalReward1 + 0.05 * batchReward1;
        self.totalReward2 = 0.95 * self.totalReward2 + 0.05 * batchReward2;

        return batchReward1, batchReward2;

    # loading modules from saved model
    def loadModel(self, savedModel):
        modules = ['rnn', 'inNet', 'outNet', 'imgNet', \
                            'predictRNN', 'predictNet'];
        # savedModel is an instance of dict
        dictSaved = isinstance(savedModel['qBot1'], dict);

        for agentName in ['aBot1', 'qBot1', 'aBot2', 'qBot2']:
            agent = getattr(self, agentName);
            for module in modules:
                if hasattr(agent, module):
                    if dictSaved: savedModule = savedModel[agentName][module];
                    else: savedModule = getattr(savedModel[agentName], module);
                    # assign to current model
                    setattr(agent, module, savedModule);

    # saving module, at given path with params and optimizer
    def saveModel(self, savePath, optimizer, params):
        modules = ['rnn', 'inNet', 'outNet', 'imgNet', \
                            'predictRNN', 'predictNet'];

        toSave = {'aBot1':{}, 'qBot1':{}, 'aBot2':{}, 'qBot2':{},\
            'params': params, 'optims':optimizer};
        for agentName in ['aBot1', 'qBot1', 'aBot2', 'qBot2']:
            agent = getattr(self, agentName);
            for module in modules:
                if hasattr(agent, module):
                    toSaveModule = getattr(agent, module);
                    toSave[agentName][module] = toSaveModule;

        # save as pickle
        with open(savePath, 'w') as fileId: pickle.dump(toSave, fileId);
