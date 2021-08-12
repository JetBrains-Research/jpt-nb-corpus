import pandas as pd
from sqlalchemy.orm.session import sessionmaker
from itertools import combinations
from typing import Tuple, Set

from ..processors import MdProcessor
from ..processors import CodeProcessor
from ..connector import Connector
from .write_to_db import write_notebook_to_db, write_features_to_db


def flatten(dictionary):
    output = dict()
    for key, value in dictionary.items():
        if isinstance(value, dict):
            output.update(flatten(value))
        else:
            output[key] = value

    return output


class Aggregator:
    def __init__(self):
        self.task_mapping = {
            'general_metrics': self.get_general_notebook_metrics,
            'complexity_metrics': self.get_mean_complexity_metrics,
            'coupling_between_cells': self.get_coupling_between_cells,
            'coupling_between_functions': self.get_coupling_between_functions,
            'coupling_between_methods': self.get_mean_coupling_between_methods,
            'functions_statistics': self.get_functions_statistics
        }
        self.cells_df = None

    @staticmethod
    def get_sets_coupling(pair: Tuple[Set, Set]) -> int:
        a, b = pair
        return len(a.intersection(b))

    def get_general_notebook_metrics(self):
        df = self.cells_df

        notebook_metrics = {
            'sloc': df.sloc.sum(),
            'comments_count': df.comments_count.sum(),
            'blank_lines_count': df.comments_count.sum(),
            'classes': df[df.classes_size > 0]. \
                classes_size. \
                dropna().astype(bool).sum(),

            'classes_comments': df[df.classes_size > 0]. \
                comments_count. \
                dropna().sum(),

            'mean_attributes_count': (df.classes_size
                                      - df.new_methods_count
                                      - df.override_methods_count).mean(),

            'mean_new_methods': df.new_methods_count.mean(),
            'mean_override_methods': df.override_methods_count.mean(),
            'comments_density': 0,
            'comments_per_class': 0,
        }

        count_markdown = True
        if count_markdown:
            default_length = 1
            notebook_metrics['comments_count'] += df[df.type == 'markdown']. \
                source. \
                apply(lambda lines: lines.count('\n') + default_length).sum()

        notebook_metrics['comments_density'] = notebook_metrics['comments_count'] \
                                               / (notebook_metrics['sloc'] + notebook_metrics['comments_count'])

        notebook_metrics['comments_per_class'] = notebook_metrics['classes_comments'] \
                                                 / max(notebook_metrics['classes'], 1)

        return notebook_metrics

    def get_coupling_between_cells(self):
        pair_combination = 2
        nan_value = float("NaN")
        cells_variables = self.cells_df.variables. \
            replace("", nan_value).dropna().apply(
            lambda ss: set(ss.split(' '))
        )

        coupling = 0
        for pair in combinations(cells_variables, pair_combination):
            coupling += self.get_sets_coupling(pair)

        return coupling

    def get_coupling_between_functions(self):
        """
        cells_df.inner_functions: Series[ List[ Set, ... , Set ] ]

        It's disgusting...
        """
        pair_combination = 2

        inner_functions_sets = []
        for list_of_sets in self.cells_df.inner_functions.dropna():
            inner_functions_sets += [functions_set for functions_set in list_of_sets]

        coupling = 0
        for pair in combinations(inner_functions_sets, pair_combination):
            coupling += self.get_sets_coupling(pair)

        return coupling

    def get_mean_coupling_between_methods(self):
        """
        Mean Coupling in cells which have methods in it
        """
        mean_coupling = self.cells_df[self.cells_df.mean_classes_coupling > 0]. \
            mean_classes_coupling.dropna().mean()

        mean_coupling = mean_coupling if mean_coupling == float("NaN") else 0
        return mean_coupling

    @staticmethod
    def flatten_list(lst):
        return [item for sublist in lst for item in sublist if item]

    def get_functions_statistics(self):  # TODO Refactor storing of defined_functions, used_functions ...
        """
        cells_df.defined_functions: Series[ String['fun_1 fun_2 ... fun_n'] ]
        cells_df.used_functions: Series[ List[ String, ... , String ] ]
        cells_df.inner_functions: Series[ List[ Set, ... , Set ] ]

        It's disgusting...
        """
        defined_functions = self.cells_df.defined_functions. \
            dropna().apply(lambda line: line.split(' '))

        defined_functions = set(self.flatten_list(defined_functions))
        used_functions = self.flatten_list([*[functions for functions in self.cells_df.used_functions.dropna()]])

        inner_functions_sets = []
        print(self.cells_df.inner_functions.dropna())
        for list_of_sets in self.cells_df.inner_functions.dropna():
            inner_functions_sets += [functions_set for functions_set in list_of_sets]

        inner_functions = set.union(*inner_functions_sets) if inner_functions_sets else set()
        all_functions = defined_functions.union(inner_functions)
        api_functions = all_functions.difference(defined_functions)

        stats = {
            'API_functions_count': len(api_functions),
            'defined_functions_count': len(defined_functions),
            'API_functions_uses': len([f for f in used_functions
                                       if f not in defined_functions]),
            'defined_functions_uses': len([f for f in used_functions
                                       if f in defined_functions])
        }
        return stats

    def get_mean_complexity_metrics(self):
        cells_metrics = ['ccn', 'npavg', 'halstead']
        notebook_metrics = {}

        for metric in cells_metrics:
            notebook_metrics[metric] = self.cells_df[metric].mean()

        return notebook_metrics

    def run_tasks(self, cells, config):
        self.cells_df = pd.DataFrame(cells).set_index('num').sort_index()

        functions = [
            function for function, executing in config.items()
            if (function in self.task_mapping.keys() and executing is True)
        ]

        features = {}
        for function in functions:
            features[function] = self.task_mapping[function]()

        return features


class Notebook(object):
    processors = [CodeProcessor, MdProcessor]
    aggregator = Aggregator()
    cells = []
    metadata = {}
    nlp = None

    def __init__(self, name, db_name=""):
        connector = Connector(name, db_name)

        self.engine = connector.engine
        self.metadata = connector.data.metadata
        self.cells = connector.data.cells

    def add_nlp_model(self, nlp):
        self.nlp = nlp

        return 1

    def write_to_db(self):
        session = sessionmaker(bind=self.engine)()

        with session as conn:
            self.metadata['id'] = write_notebook_to_db(conn, self.metadata, self.cells)

        return 1

    def run_tasks(self, config):
        for i, cell in enumerate(self.cells):
            self.cells[i] = self.processors[0](cell).process_cell(config['code']) \
                if cell['type'] == 'code' \
                else self.processors[1](cell, self.nlp).process_cell(config['markdown'])

        return self.cells

    def aggregate_tasks(self, config):
        session = sessionmaker(bind=self.engine)()
        flatten_cells = [flatten(cell) for cell in self.cells]
        features = self.aggregator.run_tasks(flatten_cells, config['notebook'])
        with session as conn:
            flatten_features = write_features_to_db(conn, self.metadata, features)

        return flatten_features
