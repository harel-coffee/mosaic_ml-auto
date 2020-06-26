# Mosaic library
import json
import logging
import os
import sys
import tempfile
from functools import partial

import numpy as np
from networkx.readwrite import json_graph
from networkx.readwrite.gpickle import write_gpickle
# scipy
from scipy.sparse import issparse
# Metric
from sklearn.metrics import (accuracy_score, balanced_accuracy_score,
                             roc_auc_score)

import pynisher
import simplejson as json
from mosaic.external.ConfigSpace import pcs_new as pcs
from mosaic_ml.evaluator import (evaluate, evaluate_generate_metadata,
                                 test_function)
from mosaic_ml.metafeatures import get_dataset_metafeature_from_openml
from mosaic_ml.model_config.encoding import OneHotEncoding
# pynisher
# Config space
from mosaic_ml.mosaic_wrapper.mosaic import SearchML
from mosaic_ml.sklearn_env import SklearnEnv


class AutoML():
    def __init__(self,
                 time_budget=3600,
                 time_limit_for_evaluation=300,
                 memory_limit=3024,
                 scoring_func="roc_auc",
                 seed=1,
                 data_manager=None,
                 exec_dir=None,
                 verbose=0,
                 ):
        self.time_budget = time_budget
        self.time_limit_for_evaluation = time_limit_for_evaluation
        self.memory_limit = memory_limit
        self.policy_arg = {"policy_name": "puct", "c": 1.3}
        self.searcher = None
        self.data_manager = data_manager
        self.seed = seed
        self.verbose = verbose
        np.random.seed(seed)
        self.logger_automl = logging.getLogger('automl')

        # Create folder dir if exec_dir is None
        if exec_dir is None:
            self.exec_dir = tempfile.mkdtemp()
        else:
            self.exec_dir = exec_dir
            os.makedirs(self.exec_dir)

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s')
        hdlr_automl = logging.FileHandler(os.path.join(self.exec_dir, "automl.log"), mode='w')
        hdlr_automl.setFormatter(formatter)
        self.logger_automl.addHandler(hdlr_automl)
        if verbose:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(formatter)
            self.logger_automl.addHandler(handler)

        self.mosaic_dir = os.path.join(self.exec_dir, "mosaic")
        self._set_scoring_func(scoring_func=scoring_func)

    def _set_scoring_func(self, scoring_func):
        if scoring_func == "balanced_accuracy":
            self.scoring_func = balanced_accuracy_score
        elif scoring_func == "accuracy":
            self.scoring_func = accuracy_score
        elif scoring_func == "roc_auc":
            self.scoring_func = roc_auc_score
        else:
            raise Exception("Score func {0} unknown".format(scoring_func))

    def adapt_search_space(self, X, y):
        import ConfigSpace.hyperparameters as CSH
        self.problem_dependant_parameter = ["preprocessor:feature_agglomeration:n_clusters",
                                            "preprocessor:kernel_pca:n_components",
                                            "preprocessor:kitchen_sinks:n_components",
                                            "preprocessor:nystroem_sampler:n_components",
                                            # "preprocessor:fast_ica:n_components"
                                            ]

        try:
            enc = OneHotEncoding.OneHotEncoder()
            nb_normal, nb_onehot_enc = np.shape(
                X)[1], np.shape(enc.fit_transform(X))[1]
        except:
            nb_normal, nb_onehot_enc = np.shape(X)[1], np.shape(X)[1]

        self.searcher.mcts.env.problem_dependant_param = self.problem_dependant_parameter

        try:
            from sklearn.naive_bayes import MultinomialNB
            MultinomialNB().fit(X, y)
            is_positive = True
        except:
            is_positive = False

        self.searcher.mcts.env.problem_dependant_value = {
            "no_encoding": nb_normal,
            "one_hot_encoding": nb_onehot_enc,
            "is_positive": is_positive
        }
        # print(self.searcher.mcts.env.problem_dependant_value)

    def prepare_ensemble(self, X, y):
        from sklearn.model_selection import train_test_split

        self.ensemble_dir = os.path.join(self.exec_dir, "ensemble_files")
        try:
            os.mkdir(self.ensemble_dir)
        except Exception as e:
            raise (e)

        _, _, y_train, y_test = train_test_split(
            X, y, test_size=0.329, random_state=self.seed)
        np.save(os.path.join(self.ensemble_dir, "y_valid.npy"), y_test)
        np.save(os.path.join(self.ensemble_dir, "y_test.npy"), y)

    def get_config_space(self, X):
        if issparse(X):
            self.logger_automl.info("Data is sparse")
            return pcs.read(open(os.path.dirname(os.path4.abspath(__file__)) + "/model_config/1_1.pcs", "r"))
        else:
            self.logger_automl.info("Data is dense")
            return pcs.read(open(os.path.dirname(os.path.abspath(__file__)) + "/model_config/1_0.pcs", "r"))

    def fit(self, X, y, categorical_features=None, initial_configurations=[]):
        return self.fit(X=X, y=y, X_test=None, y_test=None,
                        categorical_features=categorical_features, initial_configurations=initial_configurations)

    def fit(self, X, y,
            X_test = None,
            y_test=None,
            categorical_features = None,
            initial_configurations = [],
            nb_simulation=np.inf,
            policy_arg={}):
        X = np.array(X)
        y = np.array(y)

        self.logger_automl.info("-> X shape: {0}; y shape: {1}".format(str(X.shape), str(y.shape)))
        if X_test is not None:
            X_test = np.array(X_test)
            y_test = np.array(y_test)
            self.logger_automl.info("-> X shape: {0}; y shape: {1}".format(str(X_test.shape), str(y_test.shape)))

        self.logger_automl.info("-> Categorical features: {0}".format(
            str([i for i, x in enumerate(categorical_features) if x == "categorical"])))

        config_space = self.get_config_space(X)

        eval_func = partial(evaluate, X=X, y=y, score_func=self.scoring_func,
                            categorical_features=categorical_features, seed=self.seed,
                            test_data={"X_test": X_test, "y_test": y_test} if X_test is not None else {})
        # store_directory=self.ensemble_dir)

        environment = SklearnEnv(eval_func=eval_func,
                                 config_space=config_space,
                                 mem_in_mb=self.memory_limit,
                                 cpu_time_in_s=self.time_limit_for_evaluation,
                                 seed=self.seed)

        self.searcher = SearchML(environment=environment,
                                 time_budget=self.time_budget,
                                 seed=self.seed,
                                 policy_arg=self.policy_arg,
                                 exec_dir=self.mosaic_dir,
                                 verbose=self.verbose)

        self.adapt_search_space(X, y)

        try:
            res = self.searcher.run(
                nb_simulation=100000000000, initial_configurations=initial_configurations)
        except Exception as e:
            raise (e)

        return self.searcher.mcts.env.bestconfig["model"], self.searcher.mcts.env.bestconfig["validation_score"]

    def get_run_history(self):
        return self.searcher.get_history_run()

    def get_test_performance(self, X, y, categorical_features, X_test=None, y_test=None):
        test_func = pynisher.enforce_limits(mem_in_mb=self.memory_limit,
                                            cpu_time_in_s=self.time_limit_for_evaluation * 3
                                            )(test_function)
        print("Get test performance ...")
        return self.searcher.test_performance(X, y, X_test, y_test, test_func, categorical_features)

    def get_test_performance(self, X, y, categorical_features, X_test=None, y_test=None):
        test_func = pynisher.enforce_limits(mem_in_mb=self.memory_limit,
                                            cpu_time_in_s=self.time_limit_for_evaluation * 3
                                            )(test_function)
        print("Get test performance ...")
        return self.searcher.test_performance(X, y, X_test, y_test, test_func, categorical_features)
