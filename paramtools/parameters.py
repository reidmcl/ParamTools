import copy
import os
import json
import itertools
import warnings
from collections import OrderedDict, defaultdict
from functools import reduce

import numpy as np
from marshmallow import ValidationError as MarshmallowValidationError

from paramtools.schema_factory import SchemaFactory
from paramtools import utils
from paramtools.select import select_eq, select_ne, select_gt_ix, select_gt
from paramtools.exceptions import (
    SparseValueObjectsException,
    ValidationError,
    InconsistentLabelsException,
    collision_list,
    ParameterNameCollisionException,
)


class Parameters:
    defaults = None
    field_map = {}
    array_first = False
    label_to_extend = None

    def __init__(self, initial_state=None, array_first=None):
        schemafactory = SchemaFactory(self.defaults, self.field_map)
        (
            self._defaults_schema,
            self._validator_schema,
            self._data,
        ) = schemafactory.schemas()
        self.label_validators = schemafactory.label_validators
        self._stateless_label_grid = OrderedDict(
            [(name, v.grid()) for name, v in self.label_validators.items()]
        )
        self.label_grid = copy.deepcopy(self._stateless_label_grid)
        self._validator_schema.context["spec"] = self
        self._errors = {}
        self._state = initial_state or {}

        if array_first is not None:
            self.array_first = array_first
        if self.label_to_extend:
            prev_array_first = self.array_first
            self.array_first = False
            self.set_state()
            self.extend()
            if prev_array_first:
                self.array_first = True
                self.set_state()
        else:
            self.set_state()

    def set_state(self, **labels):
        """
        Sets state for the Parameters instance. The state, label_grid, and
        parameter attributes are all updated with the new state.

        Raises:
            ValidationError if the labels kwargs contain labels that are not
                specified in schema.json or if the label values fail the
                validator set for the corresponding label in schema.json.
        """

        self._set_state(**labels)

    def clear_state(self):
        """
        Reset the state of the Parameters instance.
        """
        self._state = {}
        self.label_grid = copy.deepcopy(self._stateless_label_grid)
        self.set_state()

    def view_state(self):
        """
        Access the label state of the ``Parameters`` instance.
        """
        return self._state

    def read_params(self, params_or_path):
        if isinstance(params_or_path, str) and os.path.exists(params_or_path):
            params = utils.read_json(params_or_path)
        elif isinstance(params_or_path, str):
            params = json.loads(params_or_path)
        elif isinstance(params_or_path, dict):
            params = params_or_path
        else:
            raise ValueError("params_or_path is not dict or file path")
        return params

    def adjust(self, params_or_path, raise_errors=True, extend_adj=True):
        """
        Deserialize and validate parameter adjustments. `params_or_path`
        can be a file path or a `dict` that has not been fully deserialized.
        The adjusted values replace the current values stored in the
        corresponding parameter attributes.

        Returns: parsed, validated parameters.

        Raises:
            marshmallow.exceptions.ValidationError if data is not valid.

            ParameterUpdateException if label values do not match at
                least one existing value item's corresponding label values.
        """
        params = self.read_params(params_or_path)

        # Validate user adjustments.
        parsed_params = {}
        try:
            parsed_params = self._validator_schema.load(params)
        except MarshmallowValidationError as ve:
            self._parse_errors(ve, params)

        if not self._errors:
            if self.label_to_extend is not None and extend_adj:
                extend_grid = self._stateless_label_grid[self.label_to_extend]
                for param, vos in parsed_params.items():
                    to_delete = []
                    for vo in utils.grid_sort(
                        vos, self.label_to_extend, extend_grid
                    ):
                        if self.label_to_extend in vo:
                            if (
                                vo[self.label_to_extend]
                                not in self.label_grid[self.label_to_extend]
                            ):
                                msg = (
                                    f"{param}[{self.label_to_extend}={vo[self.label_to_extend]}] "
                                    f"is not active in the current state: "
                                    f"{self.label_to_extend}= "
                                    f"{self.label_grid[self.label_to_extend]}."
                                )
                                warnings.warn(msg)
                            gt = select_gt_ix(
                                self._data[param]["value"],
                                True,
                                {
                                    self.label_to_extend: vo[
                                        self.label_to_extend
                                    ]
                                },
                                extend_grid,
                            )
                            eq = select_eq(
                                gt,
                                True,
                                utils.filter_labels(
                                    vo, drop=[self.label_to_extend, "value"]
                                ),
                            )
                            to_delete += eq
                    to_delete = [
                        dict(td, **{"value": None}) for td in to_delete
                    ]
                    # make copy of value objects since they
                    # are about to be modified
                    backup = copy.deepcopy(self._data[param]["value"])
                    try:
                        array_first = self.array_first
                        self.array_first = False
                        # delete params that will be overwritten out by extend.
                        self.adjust(
                            {param: to_delete},
                            extend_adj=False,
                            raise_errors=True,
                        )
                        # set user adjustments.
                        self.adjust(
                            {param: vos}, extend_adj=False, raise_errors=True
                        )
                        self.array_first = array_first
                        # extend user adjustments.
                        self.extend(params=[param], raise_errors=True)
                    except ValidationError:
                        self._data[param]["value"] = backup
            else:
                for param, value in parsed_params.items():
                    self._update_param(param, value)

        self._validator_schema.context["spec"] = self

        if raise_errors and self._errors:
            raise self.validation_error

        # Update attrs for params that were adjusted.
        self._set_state(params=parsed_params.keys())

        return parsed_params

    @property
    def errors(self):
        new_errors = {}
        if self._errors:
            for param, messages in self._errors["messages"].items():
                new_errors[param] = utils.ravel(messages)
        return new_errors

    @property
    def validation_error(self):
        return ValidationError(
            self._errors["messages"], self._errors["labels"]
        )

    def specification(
        self,
        use_state=True,
        meta_data=False,
        include_empty=False,
        serializable=False,
        **labels,
    ):
        """
        Query value(s) of all parameters along labels specified in
        `labels`.

        Parameters:
            - use_state: If true, use the instance's state for the select operation.
            - meta_data: If true, include information like the parameter
                description and title.
            - include_empty: If true, include parameters that do not meet the label query.
            - serializable: If true, return data that is compatible with `json.dumps`.

        Returns: serialized data of shape
            {"param_name": [{"value": val, "label0": ..., }], ...}
        """
        if use_state:
            labels.update(self._state)

        all_params = OrderedDict()
        for param in self._validator_schema.fields:
            result = self.select_eq(param, False, **labels)
            if result or include_empty:
                if meta_data:
                    param_data = self._data[param]
                    result = dict(param_data, **{"value": result})
                # Add "value" key to match marshmallow schema format.
                elif serializable:
                    result = {"value": result}
                all_params[param] = result
        if serializable:
            ser = self._defaults_schema.dump(all_params)
            # Unpack the values after serialization if meta_data not specified.
            if not meta_data:
                ser = {param: value["value"] for param, value in ser.items()}
            return ser
        else:
            return all_params

    def to_array(self, param):
        """
        Convert a Value object to an n-labelal array. The list of Value
        objects must span the specified parameter space. The parameter space
        is defined by inspecting the label validators in schema.json
        and the state attribute of the Parameters instance.

        Returns: n-labelal NumPy array.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
            SparseValueObjectsException: Value object does not span the
                entire space specified by the Order object.
        """
        value_items = self.select_eq(param, False, **self._state)
        if not value_items:
            return np.array([])
        label_order, value_order = self._resolve_order(param)
        shape = []
        for label in label_order:
            shape.append(len(value_order[label]))
        shape = tuple(shape)
        arr = np.empty(shape, dtype=self._numpy_type(param))
        # Compare len value items with the expected length if they are full.
        # In the futute, sparse objects should be supported by filling in the
        # unspecified labels.
        if not shape:
            exp_full_shape = 1
        else:
            exp_full_shape = reduce(lambda x, y: x * y, shape)
        if len(value_items) != exp_full_shape:
            # maintains label value order over value objects.
            exp_grid = list(itertools.product(*value_order.values()))
            # preserve label value order for each value object by
            # iterating over label_order.
            actual = set(
                [tuple(vo[d] for d in label_order) for vo in value_items]
            )
            missing = "\n\t".join(
                [str(d) for d in exp_grid if d not in actual]
            )
            raise SparseValueObjectsException(
                f"The Value objects for {param} do not span the specified "
                f"parameter space. Missing combinations:\n\t{missing}"
            )

        def list_2_tuple(x):
            return tuple(x) if isinstance(x, list) else x

        for vi in value_items:
            # ix stores the indices of `arr` that need to be filled in.
            ix = [[] for i in range(len(label_order))]
            for label_pos, label_name in enumerate(label_order):
                # assume value_items is dense in the sense that it spans
                # the label space.
                ix[label_pos].append(
                    value_order[label_name].index(vi[label_name])
                )
            ix = tuple(map(list_2_tuple, ix))
            arr[ix] = vi["value"]
        return arr

    def from_array(self, param, array=None):
        """
        Convert NumPy array to a Value object.

        Returns:
            Value object (shape: [{"value": val, labels:...}])

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        if array is None:
            array = getattr(self, param)
            if not isinstance(array, np.ndarray):
                raise TypeError(
                    "A NumPy Ndarray should be passed to this method "
                    "or the instance attribute should be an array."
                )
        label_order, value_order = self._resolve_order(param)
        label_values = itertools.product(*value_order.values())
        label_indices = itertools.product(
            *map(lambda x: range(len(x)), value_order.values())
        )
        value_items = []
        for dv, di in zip(label_values, label_indices):
            vi = {label_order[j]: dv[j] for j in range(len(dv))}
            vi["value"] = array[di]
            value_items.append(vi)
        return value_items

    def extend(self, label_to_extend=None, params=None, raise_errors=True):
        """
        Extend parameters along label_to_extend.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        if label_to_extend is None:
            label_to_extend = self.label_to_extend

        spec = self.specification(meta_data=True)
        if params is not None:
            spec = {
                param: self._data[param]
                for param, data in spec.items()
                if param in params
            }
        extend_grid = self._stateless_label_grid[label_to_extend]
        adjustment = defaultdict(list)
        for param, data in spec.items():
            if not any(label_to_extend in vo for vo in data["value"]):
                continue
            extended_vos = set()
            for vo in sorted(
                data["value"],
                key=lambda val: extend_grid.index(val[label_to_extend]),
            ):
                hashable_vo = utils.hashable_value_object(vo)
                if hashable_vo in extended_vos:
                    continue
                else:
                    extended_vos.add(hashable_vo)
                gt = select_gt_ix(
                    self._data[param]["value"],
                    True,
                    {label_to_extend: vo[label_to_extend]},
                    extend_grid,
                )
                eq = select_eq(
                    gt,
                    True,
                    utils.filter_labels(vo, drop=["value", label_to_extend]),
                )
                extended_vos.update(map(utils.hashable_value_object, eq))
                eq += [vo]

                defined_vals = {eq_vo[label_to_extend] for eq_vo in eq}

                missing_vals = sorted(
                    set(extend_grid) - defined_vals,
                    key=lambda val: extend_grid.index(val),
                )

                if not missing_vals:
                    continue

                extended = defaultdict(list)

                for val in missing_vals:
                    eg_ix = extend_grid.index(val)
                    if eg_ix == 0:
                        first_defined_value = min(
                            defined_vals,
                            key=lambda val: extend_grid.index(val),
                        )
                        value_objects = select_eq(
                            eq, True, {label_to_extend: first_defined_value}
                        )
                    elif extend_grid[eg_ix - 1] in extended:
                        value_objects = extended.pop(extend_grid[eg_ix - 1])
                    else:
                        prev_defined_value = extend_grid[eg_ix - 1]
                        value_objects = select_eq(
                            eq, True, {label_to_extend: prev_defined_value}
                        )
                    # In practice, value_objects has length one.
                    # Theoretically, there could be multiple if the inital value
                    # object had less labels than later value objects and thus
                    # matched multiple value objects.
                    for value_object in value_objects:
                        ext = dict(value_object, **{label_to_extend: val})
                        extended_vos.add(
                            utils.hashable_value_object(value_object)
                        )
                        extended[val].append(ext)
                        adjustment[param].append(ext)
        # Ensure that the adjust method of paramtools.Parameter is used
        # in case the child class also implements adjust.
        Parameters.adjust(
            self, adjustment, extend_adj=False, raise_errors=raise_errors
        )

    def _set_state(self, params=None, **labels):
        """
        Private method for setting the state on a Parameters instance. Internal
        methods can set which params will be updated. This is helpful when a set
        of parameters are adjusted and only their attributes need to be updated.

        """
        messages = {}
        for name, values in labels.items():
            if name not in self.label_validators:
                messages[name] = f"{name} is not a valid label."
                continue
            if not isinstance(values, list):
                values = [values]
            for value in values:
                try:
                    self.label_validators[name].deserialize(value)
                except MarshmallowValidationError as ve:
                    messages[name] = str(ve)
        if messages:
            raise ValidationError(messages, labels=None)
        self._state.update(labels)
        for label_name, label_value in self._state.items():
            if not isinstance(label_value, list):
                label_value = [label_value]
            self.label_grid[label_name] = label_value
        spec = self.specification(include_empty=True, **self._state)
        if params is not None:
            spec = {param: spec[param] for param in params}
        for name, value in spec.items():
            if name in collision_list:
                raise ParameterNameCollisionException(
                    f"The paramter name, '{name}', is already used by the Parameters object."
                )
            if self.array_first:
                setattr(self, name, self.to_array(name))
            else:
                setattr(self, name, value)

    def _resolve_order(self, param):
        """
        Resolve the order of the labels and their values by
        inspecting data in the label grid values.

        The label grid for all labels is stored in the label_grid
        attribute. The labels to be used are the ones that are specified
        for each value object. Note that the labels must be specified
        _consistently_ for all value objects, i.e. none can be added or omitted
        for any value object in the list.

        Returns:
            label_order: The label order.
            value_order: The values, in order, for each label.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        value_items = self.select_eq(param, False, **self._state)
        used = utils.consistent_labels(value_items)
        if used is None:
            raise InconsistentLabelsException(
                f"were added or omitted for some value object(s)."
            )
        label_order, value_order = [], {}
        for label_name, label_values in self.label_grid.items():
            if label_name in used:
                label_order.append(label_name)
                value_order[label_name] = label_values
        return label_order, value_order

    def _numpy_type(self, param):
        """
        Get the numpy type for a given parameter.
        """
        return (
            self._validator_schema.fields[param].nested.fields["value"].np_type
        )

    def select_eq(self, param, exact_match, **labels):
        return select_eq(self._data[param]["value"], exact_match, labels)

    def select_ne(self, param, exact_match, **labels):
        return select_ne(self._data[param]["value"], exact_match, labels)

    def select_gt(self, param, exact_match, **labels):
        return select_gt(self._data[param]["value"], exact_match, labels)

    def _update_param(self, param, new_values):
        """
        Update the current parameter values with those specified by
        the adjustment. The values that need to be updated are chosen
        by finding all value items with label values matching the
        label values specified in the adjustment. If the value is
        set to None, then that value object will be removed.

        Note: _update_param used to raise a ParameterUpdateException if one of the new
            values did not match at least one of the current value objects. However,
            this was dropped to better support the case where the parameters are being
            extended along some label to fill the parameter space. An exception could
            be raised if a new value object contains a label that is not used in the
            current value objects for the parameter. However, it seems like it could be
            expensive to check this case, especially when a project is extending parameters.
            For now, no exceptions are raised by this method.

        """
        for i in range(len(new_values)):
            curr_vals = self._data[param]["value"]
            matched_at_least_once = False
            labels_to_check = tuple(k for k in new_values[i] if k != "value")
            to_delete = []
            for j in range(len(curr_vals)):
                match = all(
                    curr_vals[j][k] == new_values[i][k]
                    for k in labels_to_check
                )
                if match:
                    matched_at_least_once = True
                    if new_values[i]["value"] is None:
                        to_delete.append(j)
                    else:
                        curr_vals[j]["value"] = new_values[i]["value"]
            if to_delete:
                # Iterate in reverse so that indices point to the correct
                # value. If iterating ascending then the values will be shifted
                # towards the front of the list as items are removed.
                for ix in sorted(to_delete, reverse=True):
                    del curr_vals[ix]
            if (
                not matched_at_least_once
                and new_values[i]["value"] is not None
            ):
                curr_vals.append(new_values[i])

    def _parse_errors(self, ve, params):
        """
        Parse the error messages given by marshmallow.

        Marshamllow error structure:

        {
            "list_param": {
                0: {
                    "value": {
                        0: [err message for first item in value list]
                        i: [err message for i-th item in value list]
                    }
                },
                i-th value object: {
                    "value": {
                        0: [...],
                        ...
                    }
                },
            }
            "nonlist_param": {
                0: {
                    "value": [err message]
                },
                ...
            }
        }

        self._errors structure:
        {
            "messages": {
                "param": [
                    ["value": {0: [msg0, msg1, ...], other_bad_ix: ...},
                     "label0": {0: msg, ...} // if errors on label values.
                ],
                ...
            },
            "label": {
                "param": [
                    {label_name: label_value, other_label_name: other_label_value},
                    ...
                    // list indices correspond to the error messages' indices
                    // of the error messages caused by the value of this value
                    // object.
                ]
            }
        }

        """
        error_info = {
            "messages": defaultdict(dict),
            "labels": defaultdict(dict),
        }

        for pname, data in ve.messages.items():
            if pname == "_schema":
                error_info["messages"]["schema"] = [
                    f"Data format error: {data}"
                ]
                continue
            if data == ["Unknown field."]:
                error_info["messages"]["schema"] = [f"Unknown field: {pname}"]
                continue
            param_data = utils.ensure_value_object(params[pname])
            error_labels = []
            formatted_errors = []
            for ix, marshmessages in data.items():
                error_labels.append(
                    utils.filter_labels(param_data[ix], drop=["value"])
                )
                formatted_errors_ix = []
                for _, messages in marshmessages.items():
                    if messages:
                        if isinstance(messages, list):
                            formatted_errors_ix += messages
                        else:
                            for _, messagelist in messages.items():
                                formatted_errors_ix += messagelist
                formatted_errors.append(formatted_errors_ix)
            error_info["messages"][pname] = formatted_errors
            error_info["labels"][pname] = error_labels

        self._errors.update(dict(error_info))
