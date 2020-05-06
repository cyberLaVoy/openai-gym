import numpy as np
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2' 

import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.keras.models import Model, clone_model
from tensorflow.keras.initializers import VarianceScaling
from tensorflow.keras.layers import Dense, Conv2D, Flatten, Input, Lambda
from tensorflow.keras.optimizers import Adam

from custom.layers import Noise, loadModel
from memory import RingBuffer

"""
gpus = tf.config.experimental.list_physical_devices('GPU')
tf.config.experimental.set_virtual_device_configuration(
    gpus[0],
    [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=1024)])
"""

class Agent:
    def __init__(self, actionSpace, modelLoad):
        self.actionSpace = actionSpace

        if modelLoad is None:
            self.model = self._createModel()
        else:
            self.model = loadModel(modelLoad)

    def _createModel(self):
        init = VarianceScaling( scale=2 )
        framesIn = Input((84,84,4), name="frames")
        actionsIn = Input((self.actionSpace,), name="mask")

        ### shared convolutional layers
        normalized = Lambda(lambda x: x / 255.0, name="prep0")(framesIn)
        # interactions between pixels
        conv0 = Conv2D(16, 8, strides=4, activation="relu", kernel_initializer=init, name="conv0")(normalized)
        conv1 = Conv2D(32, 4, strides=2, activation="relu", kernel_initializer=init, name="conv1")(conv0)
        # learned summarization
        conv2 = Conv2D(128, 3, strides=3, activation="relu", kernel_initializer=init, name="conv2")(conv1)
        flattened = Flatten()(conv2)

        ### dual architecture, where we split advantage and value learning
        # advantage split (advantage of taking action a_i in state s)
        split1 = Dense(512, activation="relu", kernel_initializer=init)(flattened)
        noise1 = Noise(stddev=1, kernel_initializer=init)(split1)
        advantage = Dense(self.actionSpace, kernel_initializer=init)(noise1)
        # value split (value of being in state s)
        split2 = Dense(512, activation="relu", kernel_initializer=init)(flattened)
        noise2 = Noise(stddev=1, kernel_initializer=init)(split2)
        value = Dense(1, kernel_initializer=init)(noise2)
        # advantage/value combined
        policy = Lambda( lambda x: x[0]+(x[1]-K.mean(x[1])) )([value, advantage])

        filtered = Lambda( lambda x: x[0]*x[1] )([policy, actionsIn])
        model = Model(inputs=[framesIn, actionsIn], outputs=filtered)

        opt = Adam( learning_rate=.0001 )
        model.compile(opt, loss="huber_loss")
        return model

    def predict(self, state):
        state = np.array( [state] )
        mask = np.ones( (1, self.actionSpace) )
        return self.model.predict( [state, mask] )

    def getAction(self, state):
        return np.argmax( self.predict(state) ) 

class ActorAgent(Agent):
    def __init__(self, game, memory, learnerChan, actorID, render, modelLoad=None):
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        super().__init__(game.getActionSpace(), modelLoad)
        self.game = game
        self.memory = memory
        self.learnerChan = learnerChan
        self.actorID = actorID
        self.render = render

    def _updateWeights(self):
        if not self.learnerChan.empty():
            weights = self.learnerChan.get()
            self.model.set_weights( weights )

    def explore(self):
        while True:
            #print("act", self.actorID)
            done = False
            s0 = self.game.reset() 
            while not done:
                if self.render:
                    self.game.render()
                # choose action
                if self.game.getFramesAfterDeath() < 2:
                    a = 1
                else:
                    a = self.getAction( s0 )
                # step
                s1, r, done, info = self.game.step(a)
                # add to memory
                self.memory.append( (s0, s1, a, r, info["life_lost"]) )
                # upkeep
                s0 = s1
                self._updateWeights()
        self.game.close()

class LearnerAgent(Agent):
    def __init__(self, memory, agentName, actionSpace, actorsChan, modelLoad=None, targetUpdateFreq=1000, actorUpdateFreq=8, gamma=0.99, sampleSize=32):
        os.environ['CUDA_VISIBLE_DEVICES'] = '0'
        super().__init__(actionSpace, modelLoad)
        self.name = agentName
        self.modelName = self.name + ".h5"
        self.memory = memory

        self.actorUpdateFreq = actorUpdateFreq
        self.actorsChan = actorsChan

        self.learnIter = 0
        self.targetUpdateFreq = targetUpdateFreq
        self.gamma = gamma
        self.sampleSize = sampleSize

        self.targetNet = clone_model(self.model)
        self._updateTarget()

    def __del__(self):
        self.model.save(self.modelName)
    
    def learn(self):
        while True:
            #print("learn")
            indices, batch, isWeights = self.memory.sample(self.sampleSize) 
            startStates, nextStates, actions, rewards, isTerminal = batch 
            actions = self._getActionsMask(actions)

            ### double achitecture, where we predict actions with online model
            onlineQvalues = self.model.predict( [nextStates, np.ones(actions.shape)] )
            predActions = np.argmax(onlineQvalues, axis=1)
            actionsMask = self._getActionsMask(predActions)

            # predict Q values from actions mask
            futureQ = self.targetNet.predict( [nextStates, actionsMask] )
            futureQ = np.max(futureQ, axis=1)
            # set what Q values should be, for given actions
            targetQ = np.zeros(actions.shape)
            targetQ[actions] = rewards + (1-isTerminal)*self.gamma*futureQ
            # so if we start in these states, the rewards should look like this
            self.model.fit([startStates, actions], targetQ, sample_weight=isWeights, verbose=0)
            
            self.learnIter += 1
            if self.learnIter % self.targetUpdateFreq == 0:
                self._updateTarget()
            if self.learnIter % self.actorUpdateFreq == 0:
                self._updateActors()

            tdError = abs( np.max(targetQ, axis=1) - np.max(onlineQvalues, axis=1) )
            self.memory.updatePriorities(indices, tdError)


    def _updateTarget(self):
        print("Updating target network...")
        self.targetNet.set_weights(self.model.get_weights()) 
    def _updateActors(self):
        print("Sending actor weights...")
        for chan in self.actorsChan:
            chan.put(self.model.get_weights())

    def _getActionsMask(self, actions):
        mask = np.zeros((actions.size, self.actionSpace)).astype(np.bool)
        mask[np.arange(actions.size), actions] = 1
        return mask

