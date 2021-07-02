#!/usr/bin/env python3

from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple, Union, cast

import numpy as np
import torch
import torch.multiprocessing as multiprocessing
from torch import Tensor
from torch.nn import Module

from captum._utils.common import _format_tensor_into_tuples, _get_module_from_name
from captum._utils.typing import TargetType, TensorOrTupleOfTensorsGeneric
from captum.attr import LayerActivation, LayerAttribution, LayerGradientXActivation
from captum.concept._core.av import AV
from captum.concept._core.cav import CAV
from captum.concept._core.concept import Concept, ConceptInterpreter
from captum.concept._utils.classifier import Classifier, DefaultClassifier
from captum.concept._utils.common import concepts_to_str


def train_cav(
    concepts: List[Concept],
    layers: Union[str, List[str]],
    classifier: Classifier,
    save_path: str,
    classifier_kwargs: Dict,
) -> Dict[str, Dict[str, CAV]]:
    r"""
    A helper function for parallel CAV computations that can be called
    from a python process.

    Please see the TCAV class documentation for further information.

    Args:
        concepts (list[Concept]): A list of Concept objects that are used
                to train a classifier and learn decision boundaries between
                those concepts for each layer defined in the `layers`
                argument.
        layers (str, list[str]): A list of layer names or a single layer
                name that is used to compute the activations of all concept
                examples per concept and train a classifier using those
                activations.
        classifier (Classifier): A custom classifier class, such as the
                Sklearn "linear_model" that allows us to train a model
                using the activation vectors extracted for a layer per concept.
                It also allows us to access trained weights of the classifier
                and the list of prediction classes.
        save_path (str): The path for storing Concept Activation
                Vectors (CAVs) and Activation Vectors (AVs).
        classifier_kwargs (dict): Additional named arguments that are passed to
                concept classifier's `train_and_eval` method.

    Returns:
        cavs (dict): A dictionary of CAV objects indexed by concept ids and
                layer names. It gives access to the weights of each concept
                in a given layer and model statistics such as accuracies
                that resulted in trained concept weights.
    """

    concepts_key = concepts_to_str(concepts)
    cavs: Dict[str, Dict[str, CAV]] = defaultdict()
    cavs[concepts_key] = defaultdict()
    layers = [layers] if isinstance(layers, str) else layers
    for layer in layers:
        # Create data loader to initialize the trainer.
        dataloader = AV.load(save_path, layer, concepts)
        assert (
            dataloader is not None
        ), "Cannot load concepts for given layer: {}".format(layer)

        # Train and evaluate the model provided in the classifier
        classifier_stats_dict = classifier.train_and_eval(
            dataloader, **classifier_kwargs
        )
        classifier_stats_dict = (
            {} if classifier_stats_dict is None else classifier_stats_dict
        )

        weights = classifier.weights()
        assert (
            weights is not None and len(weights) > 0
        ), "Model weights connot be None or empty"

        classes = classifier.classes()
        assert (
            classes is not None and len(classes) > 0
        ), "Classes cannot be None or empty"

        classes = (
            cast(torch.Tensor, classes).detach().numpy()
            if isinstance(classes, torch.Tensor)
            else classes
        )
        cavs[concepts_key][layer] = CAV(
            concepts,
            layer,
            {"weights": weights, "classes": classes, **classifier_stats_dict},
            save_path,
        )

        # Saving cavs on the disk
        cavs[concepts_key][layer].save()

    return cavs


class TCAV(ConceptInterpreter):
    r"""
    This class implements ConceptInterpreter abstract class using an
    approach called Testing with Concept Activation Vectors (TCAVs),
    as described in the paper:
        https://arxiv.org/pdf/1711.11279.pdf

    TCAV scores for a given layer, a list of concepts and input example
    are computed using the dot product between prediction's layer
    sensitivities for given input examples and Concept Activation Vectors
    (CAVs) in that same layer.

    CAVs are defined as vectors that are orthogonal to the classification boundary
    hyperplane that separate given concepts in a given layer from each other.
    For a given layer, CAVs are computed by training a classifier that uses the
    layer activation vectors for a set of concept examples as input examples and
    concept ids as corresponding input labels. Trained weights of
    that classifier represent CAVs.

    CAVs are represented as a learned weight matrix with the dimensionality
    C X F, where:
        F represents the number of input features in the classifier.
        C is the number of concepts used for the classification. Concept
        ids are used as labels for concept examples during the training.

    We can use any layer attribution algorithm to compute layer sensitivities
    of a model prediction.
    For example, the gradients of an output prediction w.r.t. the outputs of
    the layer.
    The CAVs and the Sensitivities (SENS) are used to compute the TCAV score:

        0. TCAV = CAV • SENS, a dot product between those two vectors

    The final TCAV score can be computed by aggregating the TCAV scores
    for each input concept based on the sign or magnitude of the tcav scores.

        1. sign_count_score = | TCAV > 0 | / | TCAV |
        2. magnitude_score = SUM(ABS(TCAV * (TCAV > 0))) / SUM(ABS(TCAV))
    """

    def __init__(
        self,
        model: Module,
        layers: Union[str, List[str]],
        classifier: Classifier = None,
        layer_attr_method: LayerAttribution = None,
        save_path: str = "./cav/",
        **classifier_kwargs: Any,
    ) -> None:
        r"""
        Args:
            model (Module): An instance of pytorch model that is used to compute
                    layer activations and attributions.
            layers (str, list[str]): A list of layer name(s) that are
                    used for computing concept activations (cavs) and layer
                    attributions.
            classifier (Classifier): A custom classifier class, such as the
                    Sklearn "linear_model" that allows us to train a model
                    using the activation vectors extracted for a layer per concept.
                    It also allows us to access trained weights of the model
                    and the list of prediction classes.
            layer_attr_method (LayerAttribution, optional): An instance of a layer
                    attribution algorithm that helps us to compute model prediction
                    sensitivity scores.

                    Default: None
                    If `layer_attr_method` is None, we default it to gradients
                    for the layers using `LayerGradientXActivation` layer
                    attribution algorithm.
            save_path (str, optional): The path for storing CAVs and
                    Activation Vectors (AVs).
            classifier_kwargs (any, optional): Additional arguments such as
                    `test_split_ratio` that are passed to concept `classifier`.

        Examples::
            >>>
            >>> # TCAV use example:
            >>>
            >>> # Define the concepts
            >>> stripes = Concept(0, "stripes", striped_data_iter)
            >>> random = Concept(1, "random", random_data_iter)
            >>>
            >>>
            >>> mytcav = TCAV(model=imagenet,
            >>>     layers=['inception4c', 'inception4d'])
            >>>
            >>> scores = mytcav.interpret(inputs, [[stripes, random]], target = 0)
            >>>
            For more thorough examples, please check out TCAV tutorial and test cases.
        """
        ConceptInterpreter.__init__(self, model)
        self.layers = [layers] if isinstance(layers, str) else layers
        self.concepts: Set[Concept] = set()
        self.classifier = classifier
        self.classifier_kwargs = classifier_kwargs
        self.cavs: Dict[str, Dict[str, CAV]] = defaultdict(lambda: defaultdict())
        if self.classifier is None:
            self.classifier = DefaultClassifier()
        if layer_attr_method is None:
            self.layer_attr_method = cast(
                LayerAttribution,
                LayerGradientXActivation(  # type: ignore
                    model, None, multiply_by_inputs=False
                ),
            )
        else:
            self.layer_attr_method = layer_attr_method

        self.save_path = save_path

    def generate_all_activations(self) -> None:
        r"""
        Computes layer activations for all concepts and layers that are
        defined in `self.layers` and `self.concepts` instance variables.
        """
        for concept in self.concepts:
            self.generate_activation(self.layers, concept)

    def generate_activation(self, layers: Union[str, List], concept: Concept) -> None:
        r"""
        Computes layer activations for the specified `concept` and
        the list of layer(s) `layers`.

        Args:
            layers (str, list[str]): A list of layer names or a layer name
                    that is used to compute layer activations for the
                    specific `concept`.
            concept (Concept): A single Concept object that provides access
                    to concept examples using a data iterator.
        """
        layers = [layers] if isinstance(layers, str) else layers
        layer_modules = [_get_module_from_name(self.model, layer) for layer in layers]

        layer_act = LayerActivation(self.model, layer_modules)
        assert concept.data_iter is not None, (
            "Data iterator for concept id:",
            "{} must be specified".format(concept.id),
        )
        for examples in concept.data_iter:
            activations = layer_act.attribute(examples)
            for activation, layer_name in zip(activations, layers):
                activation = torch.reshape(activation, (activation.shape[0], -1))
                AV.save(self.save_path, concept, layer_name, activation.detach())

    def generate_activations(self, concept_layers: Dict[Concept, List[str]]) -> None:
        r"""
        Computes layer activations for the concepts and layers specified in
        `concept_layers` dictionary.

        Args:
            concept_layers (dict[Concept, list[str]]): Dictionay that maps
                    Concept objects to a list of layer names to generate
                    the activations. Ex.: concept_layers =
                    {"striped": ['inception4c', 'inception4d']}
        """
        for concept in concept_layers:
            self.generate_activation(concept_layers[concept], concept)

    def load_cavs(
        self, concepts: List[Concept]
    ) -> Tuple[List[str], Dict[Concept, List[str]]]:
        r"""
        This function load CAVs as a dictionary of concept ids and
        layers. CAVs are stored in a directory located under
        `self.save_path` path, in .pkl files with the format:
        <self.save_path>/<concept_ids>-<layer_name>.pkl. Ex.:
        "/cavs/0-1-2-inception4c.pkl", where 0, 1 and 2 are concept ids.

        It returns a list of layers and a dictionary of concept-layers mapping
        for the concepts and layer that require CAV computation through training.
        This can happen if the CAVs aren't already pre-computed for a given list
        of concepts and layer.

        Args:
            concepts (list[Concept]): A list of Concept objects for which we want
                    to load the CAV.

        Returns:
            layers (list[layer]): A list of layers for which some CAVs still need
                    to be computed.
            concept_layers (dict[concept, layer]): A dictionay of concept-layers
                    mapping for which we need to perform CAV computation through
                    training.
        """

        concepts_key = concepts_to_str(concepts)

        layers = []
        concept_layers = defaultdict(list)

        for layer in self.layers:
            self.cavs[concepts_key][layer] = CAV.load(self.save_path, concepts, layer)

            # If CAV aren't loaded
            if (
                concepts_key not in self.cavs
                or layer not in self.cavs[concepts_key]
                or not self.cavs[concepts_key][layer]
            ):

                layers.append(layer)
                # For all concepts in this experimental_set
                for concept in concepts:
                    # Collect not activated layers for this concept
                    if not AV.exists(self.save_path, concept, layer):
                        concept_layers[concept].append(layer)
        return layers, concept_layers

    def compute_cavs(
        self,
        experimental_sets: List[List[Concept]],
        force_train: bool = False,
        processes: int = None,
    ):
        r"""
        This method computes CAVs for given `experiments_sets` and layers
        specified in `self.layers` instance variable. Internally, it
        trains a classifier and creates an instance of CAV class using the
        weights of the trained classifier for each experimental set.

        It also allows to compute the CAVs in parallel using python's
        multiprocessing API and the number of processes specified in
        the argument.

        Args:
            experimental_sets (list[list[Concept]]): A list of lists of concept
                    instances for which the cavs will be computed.
            force_train (bool, optional): A flag that indicates whether to
                    train the CAVs regardless of whether they are saved or not.
                    Default: False
            processes (int, optional): The number of processes to be created
                    when running in multi-processing mode. If processes > 0 then
                    CAV computation will be performed in parallel using
                    multi-processing, otherwise it will be performed sequentially
                    in a single process.
                    Default: None
        Returns:
            cavs (dict) : A mapping of concept ids and layers to CAV objects.
                    If CAVs for the concept_ids-layer pairs are present in the
                    data storage they will be loaded into the memory, otherwise
                    they will be computed using a training process and stored
                    in the data storage that can be configured using `save_path`
                    input argument.
        """

        # Update self.concepts with concepts
        for concepts in experimental_sets:
            self.concepts.update(concepts)

        concept_ids = []
        for concept in self.concepts:
            assert concept.id not in concept_ids, (
                "There is more than one instance "
                "of a concept with id {} defined in experimental sets. Please, "
                "make sure to reuse the same instance of concept".format(
                    str(concept.id)
                )
            )
            concept_ids.append(concept.id)

        if force_train:
            self.generate_all_activations()

        # List of layers per concept key (experimental_set item) to be trained
        concept_key_to_layers = defaultdict(list)

        for concepts in experimental_sets:

            concepts_key = concepts_to_str(concepts)

            # If not 'force_train', try to load a saved CAV
            if not force_train:
                layers, concept_layers = self.load_cavs(concepts)
                concept_key_to_layers[concepts_key] = layers
                # Generate activations for missing (concept, layers)
                self.generate_activations(concept_layers)
            else:
                concept_key_to_layers[concepts_key] = self.layers
        if processes is not None and processes > 1:
            pool = multiprocessing.Pool(processes)
            cavs_list = pool.starmap(
                train_cav,
                [
                    (
                        concepts,
                        concept_key_to_layers[concepts_to_str(concepts)],
                        self.classifier,
                        self.save_path,
                        self.classifier_kwargs,
                    )
                    for concepts in experimental_sets
                ],
            )
            pool.close()
            pool.join()

        else:
            cavs_list = []
            for concepts in experimental_sets:
                cavs_list.append(
                    train_cav(
                        concepts,
                        concept_key_to_layers[concepts_to_str(concepts)],
                        cast(Classifier, self.classifier),
                        self.save_path,
                        self.classifier_kwargs,
                    )
                )

        # list[Dict[concept, Dict[layer, list]]] => Dict[concept, Dict[layer, list]]
        for cavs in cavs_list:
            for c_key in cavs:
                self.cavs[c_key].update(cavs[c_key])

        return self.cavs

    def interpret(
        self,
        inputs: TensorOrTupleOfTensorsGeneric,
        experimental_sets: List[List[Concept]],
        target: TargetType = None,
        additional_forward_args: Any = None,
        processes: int = None,
        **kwargs: Any,
    ) -> Dict[str, Dict[str, Dict[str, Tensor]]]:
        r"""
        This method computes magnitude and sign-based TCAV scores for each
        experimental sets in `experimental_sets` list.
        TCAV scores are computed using a dot product between layer attribution
        scores for specific predictions and CAV vectors.

        Args:
            inputs (tensor or tuple of tensors): Inputs for which predictions
                    are performed and attributions are computed.
                    If model takes a single tensor as
                    input, a single input tensor should be provided.
                    If model takes multiple tensors as
                    input, a tuple of the input tensors should be provided.
                    It is assumed that for all given input tensors,
                    dimension 0 corresponds to the number of examples
                    (aka batch size), and if multiple input tensors are
                    provided, the examples must be aligned appropriately.
            experimental_sets (list[list[Concept]]): A list of list of Concept
                    instances.
            target (int, tuple, tensor or list, optional):  Output indices for
                    which attributions are computed (for classification cases,
                    this is usually the target class).
                    If the network returns a scalar value per example,
                    no target index is necessary.
                    For general 2D outputs, targets can be either:
                        - a single integer or a tensor containing a single
                          integer, which is applied to all input examples
                        - a list of integers or a 1D tensor, with length matching
                          the number of examples in inputs (dim 0). Each integer
                          is applied as the target for the corresponding example.
                        For outputs with > 2 dimensions, targets can be either:
                        - A single tuple, which contains #output_dims - 1
                          elements. This target index is applied to all examples.
                        - A list of tuples with length equal to the number of
                          examples in inputs (dim 0), and each tuple containing
                          #output_dims - 1 elements. Each tuple is applied as the
                          target for the corresponding example.
            additional_forward_args (Any, optional): Extra arguments that are passed to
                     model when computing the attributions for `inputs`
                     w.r.t. layer output.
                     Default: None
            processes (int, optional): The number of processes to be created. if
                    processes is larger than one then CAV computations will be
                    performed in parallel using the number of processes equal to
                    `processes`. Otherwise, CAV computations will be performed
                    sequential.
                    Default:None
            **kwargs (Any, optional): A list of arguments that are passed to layer
                    attribution algorithm's attribute method. This could be for
                    example `n_steps` in case of integrated gradients.
                    Default: None
        Returns:
            results (dict): A dictionary of sign and magnitude -based tcav scores
                    for each concept set per layer.
                    The order of TCAV scores in the resulting tensor for each
                    experimental set follows the order in which concepts
                    are passed in `experimental_sets` input argument.

        results example::
            >>> #
            >>> # scores =
            >>> # {'0-1':
            >>> #     {'inception4c':
            >>> #         {'sign_count': tensor([0.5800, 0.4200]),
            >>> #          'magnitude': tensor([0.6613, 0.3387])},
            >>> #      'inception4d':
            >>> #         {'sign_count': tensor([0.6200, 0.3800]),
            >>> #           'magnitude': tensor([0.7707, 0.2293])}}),
            >>> #  '0-2':
            >>> #     {'inception4c':
            >>> #         {'sign_count': tensor([0.6200, 0.3800]),
            >>> #          'magnitude': tensor([0.6806, 0.3194])},
            >>> #      'inception4d':
            >>> #         {'sign_count': tensor([0.6400, 0.3600]),
            >>> #          'magnitude': tensor([0.6563, 0.3437])}})})
            >>> #

        """
        self.compute_cavs(experimental_sets, processes=processes)

        scores: Dict[str, Dict[str, Dict[str, Tensor]]] = defaultdict(
            lambda: defaultdict()
        )

        # Retrieves the lengths of the experimental sets so that we can sort
        # them by the length and compute TCAV scores in batches.
        exp_set_lens = np.array(
            list(map(lambda exp_set: len(exp_set), experimental_sets)), dtype=object
        )
        exp_set_lens_arg_sort = np.argsort(exp_set_lens)

        # compute offsets using sorted lengths using their indices
        exp_set_lens_sort = exp_set_lens[exp_set_lens_arg_sort]
        exp_set_offsets_bool = [False] + list(
            exp_set_lens_sort[:-1] == exp_set_lens_sort[1:]
        )
        exp_set_offsets = []
        for i, offset in enumerate(exp_set_offsets_bool):
            if not offset:
                exp_set_offsets.append(i)

        exp_set_offsets.append(len(exp_set_lens))

        # sort experimental sets using the length of the concepts in each set
        experimental_sets_sorted = np.array(experimental_sets, dtype=object)[
            exp_set_lens_arg_sort
        ]

        for layer in self.layers:
            layer_module = _get_module_from_name(self.model, layer)
            self.layer_attr_method.layer = layer_module

            attribs = self.layer_attr_method.attribute(
                inputs,
                target=target,
                additional_forward_args=additional_forward_args,
                **kwargs,
            )

            attribs = _format_tensor_into_tuples(attribs)
            # n_inputs x n_features (2 dimensions)
            attribs = torch.cat(
                [torch.reshape(attrib, (attrib.shape[0], -1)) for attrib in attribs],
                dim=1,
            )

            # n_experiments x n_concepts x n_features (3 dimensions)
            cavs = []
            classes = []
            for concepts in experimental_sets:
                concepts_key = concepts_to_str(concepts)
                cavs_stats = cast(Dict[str, Any], self.cavs[concepts_key][layer].stats)
                cavs.append(cavs_stats["weights"].float().detach().tolist())
                classes.append(cavs_stats["classes"])

            # sort cavs and classes using the length of the concepts in each set
            cavs_sorted = np.array(cavs, dtype=object)[exp_set_lens_arg_sort]
            classes_sorted = np.array(classes, dtype=object)[exp_set_lens_arg_sort]

            i = 0
            while i < len(exp_set_offsets) - 1:
                cav_subset = np.array(
                    cavs_sorted[exp_set_offsets[i] : exp_set_offsets[i + 1]],
                    dtype=object,
                ).tolist()
                classes_subset = classes_sorted[
                    exp_set_offsets[i] : exp_set_offsets[i + 1]
                ].tolist()

                # n_experiments x n_concepts x n_features (3 dimensions)
                cav_subset = torch.tensor(cav_subset)
                cav_subset = cav_subset.to(attribs.device)
                assert len(cav_subset.shape) == 3, (
                    "cav should have 3 dimensions: n_experiments x "
                    "n_concepts x n_features."
                )

                experimental_subset_sorted = experimental_sets_sorted[
                    exp_set_offsets[i] : exp_set_offsets[i + 1]
                ]
                self._tcav_sub_computation(
                    scores,
                    layer,
                    attribs,
                    cav_subset,
                    classes_subset,
                    experimental_subset_sorted,
                )
                i += 1

        return scores

    def _tcav_sub_computation(
        self,
        scores: Dict[str, Dict[str, Dict[str, Tensor]]],
        layer: str,
        attribs: Tensor,
        cavs: Tensor,
        classes: List[List[int]],
        experimental_sets: List[List[Concept]],
    ) -> None:
        # n_inputs x n_concepts (2 dimensions)
        tcav_score = torch.matmul(attribs.float(), torch.transpose(cavs, 1, 2))
        assert len(tcav_score.shape) == 3, (
            "tcav_score should have 3 dimensions: n_experiments x "
            "n_inputs x n_concepts."
        )

        assert attribs.shape[0] == tcav_score.shape[1], (
            "attrib and tcav_score should have the same 1st and "
            "2nd dimensions respectively (n_inputs)."
        )
        # n_experiments x n_concepts
        sign_count_score = (
            torch.sum(tcav_score > 0.0, dim=1).float() / tcav_score.shape[1]
        )
        # n_experiments x n_concepts
        sign_count_score = (
            torch.sum(tcav_score > 0.0, dim=1).float() / tcav_score.shape[1]
        )

        magnitude_score = torch.sum(
            torch.abs(tcav_score * (tcav_score > 0.0).float()), dim=1
        ) / torch.sum(torch.abs(tcav_score), dim=1)

        for i, (cls_set, concepts) in enumerate(zip(classes, experimental_sets)):
            concepts_key = concepts_to_str(concepts)

            # sort classes / concepts in the order specified in concept_keys
            concept_ord = {concept.id: ci for ci, concept in enumerate(concepts)}
            new_ord = torch.tensor([concept_ord[cls] for cls in cls_set])

            # sort based on classes
            scores[concepts_key][layer] = {
                "sign_count": torch.index_select(
                    sign_count_score[i, :], dim=0, index=new_ord
                ),
                "magnitude": torch.index_select(
                    magnitude_score[i, :], dim=0, index=new_ord
                ),
            }
