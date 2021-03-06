import tensorflow as tf
import numpy as np
import config
import utils
import os
import func
from pointer_network import PointerNetwork

class Model:
    def __init__(self, word_embeddings, char_embeddings, ckpt_folder, name='model'):
        self.name = name
        self.ckpt_folder = ckpt_folder
        if self.ckpt_folder is not None:
            utils.mkdir(self.ckpt_folder)
        initializer = tf.random_uniform_initializer(-0.05, 0.05)
        with tf.variable_scope(self.name, initializer=initializer):
            self.initialize(word_embeddings, char_embeddings)


    def initialize(self, word_embeddings, char_embeddings):
        self.create_inputs()
        self.create_embeddings(word_embeddings, char_embeddings)
        self.create_encoding()
        self.create_attention()
        self.create_match()
        #self.create_pointer()
        self.create_logit()
        self.create_loss()
        self.create_optimizer()
        self.saver = tf.train.Saver(tf.global_variables(), max_to_keep=2)
        for v in tf.trainable_variables():
            print(v.name)
        print('trainable parameters: {}'.format(self.number_parameters()))


    def create_inputs(self):
        with tf.name_scope('input'):
            self.input_passage_word = tf.placeholder(tf.int32, shape=[None, None], name='passage_word')
            self.input_question_word = tf.placeholder(tf.int32, shape=[None, None], name='question_word')
            self.input_passage_char = tf.placeholder(tf.int32, shape=[None, None, None], name='passage_char')
            self.input_question_char = tf.placeholder(tf.int32, shape=[None, None, None], name='question_char')
            self.input_label = tf.placeholder(tf.float32, shape=[None, None], name='label')
            self.input_keep_prob = tf.placeholder(tf.float32, name='keep_prob')
            self.batch_size = tf.shape(self.input_passage_word)[0]
            self.passage_mask, self.passage_len = self.tensor_to_mask(self.input_passage_word)
            self.question_mask, self.question_len = self.tensor_to_mask(self.input_question_word)


    def feed(self, passage_word, passage_char, question_word, question_char, label=None, keep_prob=1.0):
        feed_dict = {
            self.input_passage_word: passage_word,
            self.input_passage_char: passage_char,
            self.input_question_word: question_word,
            self.input_question_char: question_char,
            self.input_keep_prob: keep_prob
        }
        if label is not None:
            feed_dict[self.input_label] = label
        return feed_dict


    def create_embeddings(self, word_embeddings, char_embeddings):
        with tf.name_scope('embedding'):
            with tf.name_scope('word'):
                self.word_embeddings = tf.constant(word_embeddings, name='word_embeddings')
                self.passage_word_emb = tf.nn.embedding_lookup(self.word_embeddings, self.input_passage_word)
                self.question_word_emb = tf.nn.embedding_lookup(self.word_embeddings, self.input_question_word)
            with tf.name_scope('char'):
                self.char_embeddings = tf.get_variable(name='char_embeddings', shape=np.shape(char_embeddings))
                pch = self.birnn_char_enc(self.input_passage_char, 'passage_char_embedding')
                qch = self.birnn_char_enc(self.input_question_char, 'question_char_embedding')
            self.passage_emb = tf.concat([self.passage_word_emb, pch], axis=-1)
            self.question_emb = tf.concat([self.question_word_emb, qch], axis=-1)
            tf.summary.histogram('embedding/word_embeddings', self.word_embeddings)
            tf.summary.histogram('embedding/char_embeddings', self.char_embeddings)
            tf.summary.histogram('embedding/passage_emb', self.passage_emb)
            tf.summary.histogram('embedding/question_emb', self.question_emb)


    def create_encoding(self):
        with tf.name_scope('encoding'):
            self.passage_encoding, _ = self.birnn(self.passage_emb, self.passage_len, 3, config.hidden_dim, self.input_keep_prob, 'encoding')#[batch, nwords, 500]
            self.question_encoding, _ = self.birnn(self.question_emb, self.question_len, 3, config.hidden_dim, self.input_keep_prob, 'encoding')#[batch, nwords, 500]
            tf.summary.histogram('encoding/passage_encoding', self.passage_encoding)
            tf.summary.histogram('encoding/question_encoding', self.question_encoding)


    def create_attention(self):
        with tf.name_scope('question_passage_attention'):
            qp_att = self.dot_attention(self.passage_encoding, self.question_encoding, self.question_mask, config.hidden_dim, 'question_passage_attention', self.input_keep_prob)
            self.qp_attention, _ = self.birnn(qp_att, self.passage_len, 1, config.hidden_dim, self.input_keep_prob, 'question_passage_rnn')
            tf.summary.histogram('attention/qp_attention', self.qp_attention)


    def create_logit(self):
        with tf.name_scope('logit'):
            self.logit = tf.squeeze(func.dense(self.self_match, 1, use_bias=True), [-1])
            tf.summary.histogram('logit/logit', self.logit)


    def create_loss(self):
        with tf.name_scope('loss'):
            self.loss = tf.reduce_sum(func.cross_entropy(tf.sigmoid(self.logit), self.input_label, self.passage_mask, pos_weight=3.0)) / tf.cast(self.batch_size, tf.float32)
            tf.summary.scalar('loss/loss', self.loss)


    def create_match(self):
        with tf.name_scope('self_match_attention'):
            self_att = self.dot_attention(self.qp_attention, self.qp_attention, self.passage_mask, config.hidden_dim, 'self_match_attention', self.input_keep_prob)
            self.self_match, _ = self.birnn(self_att, self.passage_len, 1, config.hidden_dim, self.input_keep_prob, 'self_match_rnn')
            tf.summary.histogram('attention/self_match', self.self_match)


    def create_pointer(self):
        with tf.name_scope('pointer'):
            #shape = tf.shape(self.question_encoding)
            #last_two_layers = tf.slice(self.question_encoding, [0, 0, -2 * config.hidden_dim], shape[:-1] + [config.hidden_dim*2])
            init_state = func.summary(self.question_encoding, config.hidden_dim, self.question_mask, self.input_keep_prob)#[batch, 150]
            pointer = PointerNetwork(self.batch_size, init_state.get_shape()[-1], self.input_keep_prob)
            self.logit_start, self.logit_end = pointer(init_state, self.self_match, config.hidden_dim, self.passage_mask)
            join_prob = tf.matmul(tf.expand_dims(self.logit_start, axis=2), tf.expand_dims(self.logit_end, axis=1))
            upper_mat = tf.matrix_band_part(join_prob, 0, -1)
            self.output_start = tf.argmax(tf.reduce_max(upper_mat, axis=2), axis=1)
            self.output_end = tf.argmax(tf.reduce_max(upper_mat, axis=1), axis=1)
            tf.summary.histogram('pointer/init_state', init_state)
            tf.summary.histogram('pointer/logit_start', self.logit_start)
            tf.summary.histogram('pointer/logit_end', self.logit_end)


    def create_optimizer(self):
        self.global_step = tf.Variable(0, trainable=False)
        self.opt = tf.train.AdamOptimizer(learning_rate=1E-3)
        grads = self.opt.compute_gradients(self.loss)
        gradients, variables = zip(*grads)
        capped_grads, _ = tf.clip_by_global_norm(gradients, 5.0)
        self.optimizer = self.opt.apply_gradients(zip(capped_grads, variables), global_step=self.global_step)


    def restore(self, sess):
        ckpt = tf.train.get_checkpoint_state(self.ckpt_folder)
        if ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
            with tf.variable_scope(self.name, reuse=tf.AUTO_REUSE):
                self.saver.restore(sess, ckpt.model_checkpoint_path)
                print('MODEL LOADED.')
        else:
            sess.run(tf.global_variables_initializer())


    def save(self, sess):
        self.saver.save(sess, os.path.join(self.ckpt_folder, 'model.ckpt'))


    def summarize(self, writer):
        self.summary = tf.summary.merge_all()


    def tensor_to_mask(self, value):
        mask = tf.cast(value, tf.bool)
        return tf.cast(mask, tf.float32), tf.reduce_sum(tf.cast(mask, tf.int32), axis=-1)


    def birnn(self, input, length, num_layers, hidden_dim, keep_prob, scope):
        cells_fw = []
        cells_bk = []
        if hidden_dim is None:
            hidden_dim = input.get_shape()[-1]
        with tf.variable_scope(scope):
            for layer in range(num_layers):
                with tf.variable_scope('layer{}'.format(layer)):
                    cell_fw = tf.nn.rnn_cell.BasicLSTMCell(hidden_dim, state_is_tuple=True, reuse=tf.AUTO_REUSE)
                    cell_bk = tf.nn.rnn_cell.BasicLSTMCell(hidden_dim, state_is_tuple=True, reuse=tf.AUTO_REUSE)
                    cell_fw = tf.nn.rnn_cell.DropoutWrapper(cell_fw, input_keep_prob=1.0, output_keep_prob=keep_prob)
                    cell_bk = tf.nn.rnn_cell.DropoutWrapper(cell_bk, input_keep_prob=1.0, output_keep_prob=keep_prob)
                    cells_fw.append(cell_fw)
                    cells_bk.append(cell_bk)
            output, state_fw, state_bk = tf.contrib.rnn.stack_bidirectional_dynamic_rnn(cells_fw, cells_bk, input, dtype=tf.float32, sequence_length=length)
        state_fw = state_fw[-1].h
        state_bk = state_bk[-1].h
        states = tf.concat([state_fw, state_bk], axis=-1)
        return output, states


    def birnn_char_enc(self, char_id, name):
        with tf.name_scope(name):
            pch = tf.nn.dropout(tf.nn.embedding_lookup(self.char_embeddings, char_id), self.input_keep_prob, name='embedding_lookup')
            shape = tf.shape(pch)
            nbatches = shape[0]
            nwords = shape[1]
            nchars = shape[2]
            ndim = pch.get_shape()[3]
            _, slen = self.tensor_to_mask(char_id)
            slen = tf.reshape(slen, [nbatches*nwords])
            reshaped_pch = tf.reshape(pch, [nbatches*nwords, nchars, ndim])
            _, enc = self.birnn(reshaped_pch, slen, 1, config.char_hidden_dim, self.input_keep_prob, 'char_embedding')
            enc = tf.reshape(enc, [nbatches, nwords, enc.get_shape()[-1]])
            return enc


    def dot_attention(self, value, memory, mask, hidden_dim, scope, keep_prob):
        value = tf.nn.dropout(value, keep_prob)#[batch, plen, 500]
        memory = tf.nn.dropout(memory, keep_prob)#[batch, qlen, 500]
        with tf.variable_scope(scope):
            with tf.variable_scope('attention'):
                dense_value = tf.nn.relu(func.dense(value, config.hidden_dim, False, 'value'))#[batch, plen, 75]
                dense_memory = tf.nn.relu(func.dense(memory, config.hidden_dim, False, 'memory'))#[batch, qlen, 75]
                coref = tf.matmul(dense_value, tf.transpose(dense_memory, [0, 2, 1])) / (hidden_dim**0.5)#[batch, plen, qlen]
                #alpha = func.softmax(coref, tf.expand_dims(mask, axis=1))#[batch, plen, qlen]
                alpha = tf.nn.sigmoid(coref) * tf.expand_dims(mask, axis=1)
                attention = tf.matmul(alpha, memory, name='paired_attention')#[batch, plen, 500]
                pair = tf.concat([value, attention], axis=-1)#[batch, plen, 1000]
            with tf.variable_scope('gate'):
                last_dim = pair.get_shape()[-1]#1000
                d_pair = tf.nn.dropout(pair, keep_prob=keep_prob)
                gate = tf.nn.sigmoid(func.dense(d_pair, last_dim, use_bias=False))#[batch, plen, 1000]
                return pair * gate


    def one_hot(self, label):
        return tf.one_hot(label, tf.shape(self.logit)[-1], dtype=tf.float32)


    def number_parameters(self):
        total_parameters = 0
        for variable in tf.trainable_variables():
            # shape is an array of tf.Dimension
            shape = variable.get_shape()
            variable_parameters = 1
            for dim in shape:
                variable_parameters *= dim.value
            total_parameters += variable_parameters
        return total_parameters


if __name__ == '__main__':
    word_embeddings = utils.load_json(config.word_embeddings_file)
    char_embeddings = utils.load_json(config.char_embeddings_file)
    model = Model(word_embeddings, char_embeddings, None)