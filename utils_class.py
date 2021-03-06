# 
# utils_class.py
# ==============================================================================
import os
import sys
import json
import threading
from abc import ABC, abstractmethod

import h5py
import numpy as np
import six
import tqdm

from keras.models import load_model
from keras.callbacks import ModelCheckpoint
from six.moves import cPickle

from misc import get_logger, Option
from network import MainNet, top1_acc, customLoss

opt = Option('./config.json')
if six.PY2:
    cate1 = json.loads(open('../cate1.json').read())
else:
    cate1 = json.loads(open('../cate1.json', 'rb').read().decode('utf-8'))


class ClassifierBone(ABC):
    def __init__(self, net_name):
        self.logger = get_logger(net_name)
        self.num_classes = 0

    def get_inverted_cate1(self, cate1):
        inv_cate1 = {}
        for d in ['b', 'm', 's', 'd']:
            inv_cate1[d] = {v: k for k, v in six.iteritems(cate1[d])}
        return inv_cate1
    
    def train(self, data_root, out_dir):
        data_path = os.path.join(data_root, 'data.h5py')
        meta_path = os.path.join(data_root, 'meta')
        data = h5py.File(data_path, 'r')
        meta = cPickle.loads(open(meta_path, 'rb').read())
        self.weight_fname = os.path.join(out_dir, 'weights')
        self.model_fname = os.path.join(out_dir, 'model')
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)

        self.logger.info('# of classes: %s' % len(meta['y_vocab']))
        self.num_classes = len(meta['y_vocab'])

        train = data['train']
        dev = data['dev']

        self.logger.info('# of train samples: %s' % train['cate'].shape[0])
        self.logger.info('# of dev samples: %s' % dev['cate'].shape[0])

        checkpoint = ModelCheckpoint(self.weight_fname, monitor='val_loss',
                                     save_best_only=True, mode='min', period=1)

        main_net = MainNet()
        model = main_net.get_model(self.num_classes)

        total_train_samples = train['embd_word'].shape[0]
        train_gen = self.get_sample_generator(train,
                                              batch_size=opt.batch_size)
        self.steps_per_epoch = int(np.ceil(total_train_samples / float(opt.batch_size)))

        total_dev_samples = dev['embd_word'].shape[0]
        dev_gen = self.get_sample_generator(dev,
                                            batch_size=opt.batch_size)
        self.validation_steps = int(np.ceil(total_dev_samples / float(opt.batch_size)))

        model.fit_generator(generator=train_gen,
                            steps_per_epoch=self.steps_per_epoch,
                            epochs=opt.num_epochs_train,
                            validation_data=dev_gen,
                            validation_steps=self.validation_steps,
                            shuffle=True,
                            callbacks=[checkpoint])

    def get_sample_generator(self, ds, batch_size, raise_stop_event=False):
        left, limit = 0, ds['embd_word'].shape[0]
        while True:
            right = min(left + batch_size, limit)
            X = [ds[t][left:right] for t in ['embd_word', 'img_feat']]
            Y = ds['cate'][left:right]
            
            yield X, Y
            left = right
            if right == limit:
                left = 0
                if raise_stop_event:
                    raise StopIteration
    
    def write_prediction_result(self, data, pred_y, pred_y_top_n, confid_top_n, meta, out_path, readable):
        y2l = {i: s for s, i in six.iteritems(meta['y_vocab'])}
        y2l = list(map(lambda x: x[1], sorted(y2l.items(), key=lambda x: x[0])))
        inv_cate1 = self.get_inverted_cate1(cate1)
        rets = {}

        pid2label = {}
        for pid, y, y_top_n, p_top_n in zip(data['pid'], pred_y, pred_y_top_n, confid_top_n):
            if six.PY3:
                pid = pid.decode('utf-8')
            aaa = "{}".format(pid)

            for i, j in zip(y_top_n, p_top_n):
                ll = y2l[i] # Lower case of LL
                tkns = list(map(int, ll.split('>')))
                aaa += "\t{}\t{}\t{}\t{}\t{}".format(j, tkns[0], tkns[1], tkns[2], tkns[3])
            pid2label[pid] = aaa           
            
        no_answer = '{pid}\t-1\t-1\t-1\t-1'
        with open(out_path, 'w') as fout:
            for pid in pid2label:
                ans = pid2label.get(pid, no_answer.format(pid=pid))
                # if ans == no_answer.format(pid=pid):
                fout.write(ans)
                fout.write('\n')
    
    def predict(self, data_root, model_root, test_root, test_div, out_path, readable=False):
        meta_path = os.path.join(data_root, 'meta')
        meta = cPickle.loads(open(meta_path, 'rb').read())

        model_fname = os.path.join(model_root, 'weights')
        self.logger.info('# of classes(train): %s' % len(meta['y_vocab']))
        model = load_model(model_fname,
                           custom_objects={'top1_acc': top1_acc, 'customLoss': customLoss})

        test_path = os.path.join(test_root, 'data.h5py')
        test_data = h5py.File(test_path, 'r')

        test = test_data[test_div]
        batch_size = opt.batch_size
        pred_y = []
        pred_y_top_n = []
        confid_top_n = []
        n = 5
        test_gen = ThreadsafeIter(self.get_sample_generator(test, batch_size, raise_stop_event=True))
        total_test_samples = test['embd_word'].shape[0]
         
        with tqdm.tqdm(total=total_test_samples) as pbar:
            for chunk in test_gen:
                X, _ = chunk
                _pred_y = model.predict(X)
                pred_y.extend([np.argmax(y) for y in _pred_y])
                
                pred_y_top_n.extend([np.argsort(y)[-n:][::-1] for y in _pred_y])
                confid_top_n.extend([y[np.argsort(y)[-n:][::-1]] for y in _pred_y])
                
                pbar.update(X[0].shape[0])
        self.write_prediction_result(test, pred_y, pred_y_top_n, confid_top_n, meta, out_path, readable=readable)

    
class ThreadsafeIter(object):
    def __init__(self, it):
        self._it = it
        self._lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            return next(self._it)

    def next(self):
        with self._lock:
            return self._it.next()
