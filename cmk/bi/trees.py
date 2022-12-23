#!/usr/bin/env python3
# Copyright (C) 2020 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from __future__ import annotations

from typing import Any, NamedTuple, Set

from marshmallow import fields, pre_dump
from marshmallow_oneofschema import OneOfSchema

from livestatus import SiteId

from cmk.utils.caching import instance_method_lru_cache
from cmk.utils.defines import host_state_name, service_state_name
from cmk.utils.type_defs import HostName, HostState, ServiceName, ServiceState

from cmk.bi.aggregation_functions import BIAggregationFunctionSchema
from cmk.bi.lib import (
    ABCBIAggregationFunction,
    ABCBICompiledNode,
    ABCBISearcher,
    ABCBIStatusFetcher,
    BIAggregationComputationOptions,
    BIAggregationGroups,
    BIHostSpec,
    BIHostStatusInfoRow,
    BIServiceWithFullState,
    BIStates,
    CompiledNodeKind,
    create_nested_schema_for_class,
    NodeComputeResult,
    NodeIdentifierInfo,
    NodeResultBundle,
    ReqConstant,
    ReqList,
    ReqNested,
    ReqString,
    RequiredBIElement,
)
from cmk.bi.node_vis import (
    BIAggregationVisualizationSchema,
    BINodeVisBlockStyleSchema,
    BINodeVisLayoutStyleSchema,
)
from cmk.bi.rule_interface import BIRuleProperties
from cmk.bi.schema import Schema

#   .--Leaf----------------------------------------------------------------.
#   |                         _                __                          |
#   |                        | |    ___  __ _ / _|                         |
#   |                        | |   / _ \/ _` | |_                          |
#   |                        | |__|  __/ (_| |  _|                         |
#   |                        |_____\___|\__,_|_|                           |
#   |                                                                      |
#   +----------------------------------------------------------------------+


class BICompiledLeaf(ABCBICompiledNode):
    @classmethod
    def kind(cls) -> CompiledNodeKind:
        return "leaf"

    def __init__(
        self,
        host_name: HostName,
        site_id: str,
        service_description: ServiceName | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.site_id = SiteId(site_id)
        self.required_hosts = [(self.site_id, host_name)]
        self.host_name = host_name
        self.service_description = service_description

    def _get_comparable_name(self) -> str:
        return ":".join([self.site_id or "", self.host_name, self.service_description or ""])

    def get_identifiers(self, parent_id: tuple, used_ids: Set[tuple]) -> list[NodeIdentifierInfo]:
        own_id = (1, self.host_name, self.service_description)
        while (*parent_id, own_id) in used_ids:
            own_id = (own_id[0] + 1, self.host_name, self.service_description)
        my_id = (*parent_id, own_id)
        used_ids.add(my_id)
        return [NodeIdentifierInfo(my_id, self)]

    def parse_schema(self, schema_config: dict) -> None:
        self.site_id = schema_config["site_id"]
        self.host_name = schema_config["host_name"]
        self.service_description = schema_config["service_description"]

    def services_of_host(self, host_name: HostName) -> set[ServiceName]:
        if host_name == self.host_name and self.service_description:
            return {self.service_description}
        return set()

    def compile_postprocess(
        self,
        bi_branch_root: ABCBICompiledNode,
        services_of_host: dict[HostName, set[ServiceName]],
        bi_searcher: ABCBISearcher,
    ) -> list[ABCBICompiledNode]:
        return [self]

    @instance_method_lru_cache()
    def required_elements(self) -> set[RequiredBIElement]:
        return {RequiredBIElement(self.site_id, self.host_name, self.service_description)}

    def __str__(self) -> str:
        return "BICompiledLeaf[Site {}, Host: {}, Service {}]".format(
            self.site_id,
            self.host_name,
            self.service_description,
        )

    def __repr__(self):
        return f"{self} / frozen: {self.frozen_marker}"

    def compute(
        self,
        computation_options: BIAggregationComputationOptions,
        bi_status_fetcher: ABCBIStatusFetcher,
        use_assumed: bool = False,
    ) -> NodeResultBundle | None:
        host_downtime_depth, entity = self._get_entity(bi_status_fetcher)
        if (
            entity is None
            or host_downtime_depth is None
            or entity.state is None
            or entity.hard_state is None
        ):
            # Note: An entity state of None may be generated by the availability
            #       There might be service information, but no host information available
            #       A state of None will be treated as "missing" - the leaf does not exist
            #       For frozen aggregations the leaf remains, but reports the state Unknown
            if computation_options.freeze_aggregations:
                return NodeResultBundle(
                    NodeComputeResult(
                        3,
                        0,
                        False,
                        f"{'Host ' if self.service_description is None else 'Service'} not found",
                        False,
                        {},
                        {},
                    ),
                    None,
                    [],
                    self,
                )
            return None

        # Downtime
        downtime_state = 0
        if entity.scheduled_downtime_depth != 0 or host_downtime_depth > 0:
            downtime_state = 1 if computation_options.escalate_downtimes_as_warn else 2

        # State
        if entity.has_been_checked:
            state = entity.hard_state if computation_options.use_hard_states else entity.state
            # Since we need an equalized state mapping, map host state DOWN to CRIT
            if self.service_description is None:
                state = self._map_hoststate_to_bistate(state)
        else:
            state = BIStates.PENDING

        # Assumed
        assumed_result = None
        if use_assumed:
            assumed_state = bi_status_fetcher.assumed_states.get(
                RequiredBIElement(self.site_id, self.host_name, self.service_description)
            )
            if assumed_state is not None:
                # Make the i18n call explicit for our tooling
                _ = bi_status_fetcher.sites_callback.translate
                assumed_result = NodeComputeResult(
                    int(assumed_state),
                    downtime_state,
                    bool(entity.acknowledged),
                    _("Assumed to be %s") % self._get_state_name(assumed_state),
                    entity.in_service_period,
                    {},
                    {},
                )

        return NodeResultBundle(
            NodeComputeResult(
                state,
                downtime_state,
                bool(entity.acknowledged),
                entity.plugin_output,
                bool(entity.in_service_period),
                {},
                {},
            ),
            assumed_result,
            [],
            self,
        )

    def _map_hoststate_to_bistate(self, host_state: HostState) -> int:
        if host_state == BIStates.HOST_UP:
            return BIStates.OK
        if host_state == BIStates.HOST_DOWN:
            return BIStates.CRIT
        if host_state == BIStates.HOST_UNREACHABLE:
            return BIStates.UNKNOWN
        return BIStates.UNKNOWN

    def _get_state_name(self, state: HostState | ServiceState) -> str:
        return service_state_name(state) if self.service_description else host_state_name(state)

    def _get_entity(
        self, bi_status_fetcher: ABCBIStatusFetcher
    ) -> tuple[int | None, BIHostStatusInfoRow | BIServiceWithFullState | None]:
        assert self.site_id is not None
        entity = bi_status_fetcher.states.get(BIHostSpec(self.site_id, self.host_name))
        if not entity:
            return None, None
        if self.service_description is None:
            return entity.scheduled_downtime_depth, entity
        return entity.scheduled_downtime_depth, entity.services_with_fullstate.get(
            self.service_description
        )

    @classmethod
    def schema(cls) -> type[BICompiledLeafSchema]:
        return BICompiledLeafSchema

    def serialize(self):
        return {
            "type": self.kind(),
            "required_hosts": list(
                map(lambda x: {"site_id": x[0], "host_name": x[1]}, self.required_hosts)
            ),
            "site_id": self.site_id,
            "host_name": self.host_name,
            "service_description": self.service_description,
        }


class BISiteHostPairSchema(Schema):
    site_id = ReqString()
    host_name = ReqString()

    @pre_dump
    def pre_dumper(self, obj: tuple, many: bool = False) -> dict:
        # Convert aggregations and rules to list
        return {"site_id": obj[0], "host_name": obj[1]}


class BICompiledLeafSchema(Schema):
    type = ReqConstant(BICompiledLeaf.kind())
    required_hosts = ReqList(fields.Nested(BISiteHostPairSchema))
    site_id = ReqString()
    host_name = ReqString()
    service_description = fields.String()


#   .--Rule----------------------------------------------------------------.
#   |                         ____        _                                |
#   |                        |  _ \ _   _| | ___                           |
#   |                        | |_) | | | | |/ _ \                          |
#   |                        |  _ <| |_| | |  __/                          |
#   |                        |_| \_\\__,_|_|\___|                          |
#   |                                                                      |
#   +----------------------------------------------------------------------+


class BICompiledRule(ABCBICompiledNode):
    @classmethod
    def kind(cls) -> CompiledNodeKind:
        return "rule"

    def __init__(
        self,
        rule_id: str,
        pack_id: str,
        nodes: list[ABCBICompiledNode],
        required_hosts: list[tuple[SiteId, HostName]],
        properties: BIRuleProperties,
        aggregation_function: ABCBIAggregationFunction,
        node_visualization: dict[str, Any],
    ):
        super().__init__()
        self.id = rule_id
        self.pack_id = pack_id
        self.required_hosts = required_hosts
        self.nodes = nodes
        self.properties = properties
        self.aggregation_function = aggregation_function
        self.node_visualization = node_visualization

    def __str__(self) -> str:
        return "BICompiledRule[%s, %d rules, %d leaves %d remaining]" % (
            self.properties.title,
            len([x for x in self.nodes if x.kind() == "rule"]),
            len([x for x in self.nodes if x.kind() == "leaf"]),
            len([x for x in self.nodes if x.kind() == "remaining"]),
        )

    def __repr__(self):
        return f"repr(self) / frozen: {self.frozen_marker}"

    def _get_comparable_name(self) -> str:
        return self.properties.title

    def get_identifiers(self, parent_id: tuple, used_ids: Set[tuple]) -> list[NodeIdentifierInfo]:
        idents = []
        own_id = (1, self.properties.title)
        while (*parent_id, own_id) in used_ids:
            own_id = (own_id[0] + 1, own_id[1])
        my_id = (*parent_id, own_id)
        idents.append(NodeIdentifierInfo(my_id, self))
        used_ids.add(my_id)
        for node in self.nodes:
            idents.extend(node.get_identifiers(my_id, used_ids))
        return idents

    def compile_postprocess(
        self,
        bi_branch_root: ABCBICompiledNode,
        services_of_host: dict[HostName, set[ServiceName]],
        bi_searcher: ABCBISearcher,
    ) -> list[ABCBICompiledNode]:
        self.nodes = [
            res
            for node in self.nodes
            for res in node.compile_postprocess(bi_branch_root, services_of_host, bi_searcher)
        ]
        # Clear required elements cache, since the number of nodes might have changed
        # NOTE: We need this suppression because of the instance_method_lru_cache hack, which magically adds things to its wrapped method. :-/
        self.required_elements.cache_clear()  # type: ignore[attr-defined]
        return [self]

    @instance_method_lru_cache()
    def required_elements(self) -> set[RequiredBIElement]:
        return {result for node in self.nodes for result in node.required_elements()}

    def services_of_host(self, host_name: HostName) -> set[ServiceName]:
        return {result for node in self.nodes for result in node.services_of_host(host_name)}

    def get_required_hosts(self) -> set[BIHostSpec]:
        return {
            BIHostSpec(element.site_id, element.host_name) for element in self.required_elements()
        }

    def compute(
        self,
        computation_options: BIAggregationComputationOptions,
        bi_status_fetcher: ABCBIStatusFetcher,
        use_assumed: bool = False,
    ) -> NodeResultBundle | None:
        bundled_results = [
            bundle
            for bundle in [
                node.compute(computation_options, bi_status_fetcher, use_assumed)
                for node in self.nodes
            ]
            if bundle is not None
        ]
        if not bundled_results:
            return None
        actual_result = self._process_node_compute_result(
            [x.actual_result for x in bundled_results], computation_options
        )

        if not use_assumed:
            return NodeResultBundle(actual_result, None, bundled_results, self)

        assumed_result_items = []
        for bundle in bundled_results:
            assumed_result_items.append(
                bundle.assumed_result if bundle.assumed_result is not None else bundle.actual_result
            )
        assumed_result = self._process_node_compute_result(
            assumed_result_items, computation_options
        )
        return NodeResultBundle(actual_result, assumed_result, bundled_results, self)

    def _process_node_compute_result(
        self, results: list[NodeComputeResult], computation_options: BIAggregationComputationOptions
    ) -> NodeComputeResult:
        state = self.aggregation_function.aggregate([result.state for result in results])

        downtime_state = self.aggregation_function.aggregate(
            [result.downtime_state for result in results]
        )
        if downtime_state > 0:
            downtime_state = 2 if computation_options.escalate_downtimes_as_warn else 1

        is_acknowledged = False
        if state != 0:
            is_acknowledged = (
                self.aggregation_function.aggregate(
                    [0 if result.acknowledged else result.state for result in results]
                )
                == 0
            )

        in_service_period = (
            self.aggregation_function.aggregate(
                [0 if result.in_service_period else 2 for result in results]
            )
            == 0
        )

        return NodeComputeResult(
            state,
            downtime_state,
            is_acknowledged,
            # TODO: fix str casting in later commit
            self.properties.state_messages.get(str(state), ""),
            in_service_period,
            self.properties.state_messages,
            {},
        )

    @classmethod
    def schema(cls) -> type[BICompiledRuleSchema]:
        return BICompiledRuleSchema

    def serialize(self):
        return {
            "id": self.id,
            "pack_id": self.pack_id,
            "type": self.kind(),
            "required_hosts": list(
                map(lambda x: {"site_id": x[0], "host_name": x[1]}, self.required_hosts)
            ),
            "nodes": [node.serialize() for node in self.nodes],
            "aggregation_function": self.aggregation_function.serialize(),
            "node_visualization": self.node_visualization,
            "properties": self.properties.serialize(),
        }


class BICompiledRuleSchema(Schema):
    id = ReqString()
    pack_id = ReqString()
    type = ReqConstant(BICompiledRule.kind())
    required_hosts = ReqList(fields.Nested(BISiteHostPairSchema))
    nodes = ReqList(fields.Nested("BIResultSchema"))
    aggregation_function = ReqNested(
        BIAggregationFunctionSchema,
        example={"type": "worst", "count": 2, "restrict_state": 1},
    )
    node_visualization = ReqNested(
        BINodeVisLayoutStyleSchema, example=BINodeVisBlockStyleSchema().dump({})
    )
    properties = ReqNested("BIRulePropertiesSchema", example={})


#   .--Remaining-----------------------------------------------------------.
#   |           ____                      _       _                        |
#   |          |  _ \ ___ _ __ ___   __ _(_)_ __ (_)_ __   __ _            |
#   |          | |_) / _ \ '_ ` _ \ / _` | | '_ \| | '_ \ / _` |           |
#   |          |  _ <  __/ | | | | | (_| | | | | | | | | | (_| |           |
#   |          |_| \_\___|_| |_| |_|\__,_|_|_| |_|_|_| |_|\__, |           |
#   |                                                     |___/            |
#   +----------------------------------------------------------------------+


class BIRemainingResult(ABCBICompiledNode):
    # The BIRemainingResult lacks a serializable schema, since it is resolved into
    # BICompiledLeaf(s) during the compilation
    @classmethod
    def kind(cls) -> CompiledNodeKind:
        return "remaining"

    def __init__(self, host_names: list[HostName]) -> None:
        super().__init__()
        self.host_names = host_names

    def _get_comparable_name(self) -> str:
        return ""

    def compile_postprocess(
        self,
        bi_branch_root: ABCBICompiledNode,
        services_of_host: dict[HostName, set[ServiceName]],
        bi_searcher: ABCBISearcher,
    ) -> list[ABCBICompiledNode]:
        postprocessed_nodes: list[ABCBICompiledNode] = []
        for host_name in self.host_names:
            site_id = bi_searcher.hosts[host_name].site_id
            used_services = services_of_host.get(host_name, set())
            for service_description in set(bi_searcher.hosts[host_name].services) - used_services:
                postprocessed_nodes.append(
                    BICompiledLeaf(
                        host_name=host_name,
                        service_description=service_description,
                        site_id=site_id,
                    )
                )
        postprocessed_nodes.sort()
        return postprocessed_nodes

    @instance_method_lru_cache()
    def required_elements(self) -> set[RequiredBIElement]:
        return set()

    def services_of_host(self, host_name: HostName) -> set[ServiceName]:
        return set()

    def compute(
        self,
        computation_options: BIAggregationComputationOptions,
        bi_status_fetcher: ABCBIStatusFetcher,
        use_assumed: bool = False,
    ) -> NodeResultBundle | None:
        return None

    def serialize(self) -> dict[str, Any]:
        return {}


#   .--Aggregation---------------------------------------------------------.
#   |         _                                    _   _                   |
#   |        / \   __ _  __ _ _ __ ___  __ _  __ _| |_(_) ___  _ __        |
#   |       / _ \ / _` |/ _` | '__/ _ \/ _` |/ _` | __| |/ _ \| '_ \       |
#   |      / ___ \ (_| | (_| | | |  __/ (_| | (_| | |_| | (_) | | | |      |
#   |     /_/   \_\__, |\__, |_|  \___|\__, |\__,_|\__|_|\___/|_| |_|      |
#   |             |___/ |___/          |___/                               |
#   +----------------------------------------------------------------------+


class FrozenBIInfo(NamedTuple):
    based_on_aggregation_id: str
    based_on_branch_title: str


class BICompiledAggregation:
    def __init__(
        self,
        aggregation_id: str,
        branches: list[BICompiledRule],
        computation_options: BIAggregationComputationOptions,
        aggregation_visualization: dict[str, Any],
        groups: BIAggregationGroups,
    ):
        self.id = aggregation_id
        self.frozen_info: FrozenBIInfo | None = None
        self.branches = branches
        self.computation_options = computation_options
        self.aggregation_visualization = aggregation_visualization
        self.groups = groups

    def compute_branches(
        self, branches: list[BICompiledRule], bi_status_fetcher: ABCBIStatusFetcher
    ) -> list[NodeResultBundle]:
        assumed_state_ids = set(bi_status_fetcher.assumed_states)
        aggregation_results = []
        for bi_compiled_branch in branches:
            required_elements = bi_compiled_branch.required_elements()
            compute_assumed_state = any(assumed_state_ids.intersection(required_elements))
            result = bi_compiled_branch.compute(
                self.computation_options, bi_status_fetcher, use_assumed=compute_assumed_state
            )
            if result is not None:
                aggregation_results.append(result)
        return aggregation_results

    def convert_result_to_legacy_format(self, node_result_bundle: NodeResultBundle) -> dict:
        def generate_state(item):
            if not item:
                return None
            return {
                "state": item.state,
                "acknowledged": item.acknowledged,
                "in_downtime": item.downtime_state > 0,
                "in_service_period": item.in_service_period,
                "output": item.output,
            }

        def create_tree_state(bundle: NodeResultBundle, is_toplevel: bool = False) -> tuple:
            response = []
            response.append(generate_state(bundle.actual_result))
            response.append(generate_state(bundle.assumed_result))
            if is_toplevel:
                response.append(self.create_aggr_tree(bundle.instance))
            else:
                response.append(self.eval_result_node(bundle.instance))
            if bundle.nested_results:
                response.append(list(map(create_tree_state, bundle.nested_results)))
            return tuple(response)

        bi_compiled_branch = node_result_bundle.instance

        response = {
            "aggr_tree": self.create_aggr_tree(bi_compiled_branch),
            "aggr_treestate": create_tree_state(node_result_bundle, is_toplevel=True),
            "aggr_state": generate_state(node_result_bundle.actual_result),
            "aggr_assumed_state": generate_state(node_result_bundle.assumed_result),
            "aggr_effective_state": generate_state(
                node_result_bundle.assumed_result
                if node_result_bundle.assumed_result
                else node_result_bundle.actual_result
            ),
            "aggr_id": bi_compiled_branch.properties.title,
            "aggr_name": bi_compiled_branch.properties.title,
            "aggr_output": node_result_bundle.actual_result.output,
            "aggr_hosts": bi_compiled_branch.required_hosts,
            "aggr_type": "multi",
            "aggr_group": "dummy",  # dummy, will be set later on within the old bi madness
            # Required in availability
            "aggr_compiled_aggregation": self,
            "aggr_compiled_branch": bi_compiled_branch,
        }

        response["tree"] = response["aggr_tree"]
        return response

    def create_aggr_tree(self, bi_compiled_branch: BICompiledRule) -> dict:
        response = self.eval_result_node(bi_compiled_branch)
        response["aggr_group_tree"] = self.groups.names
        response["aggr_group_tree"] += ["/".join(x) for x in self.groups.paths]
        response["aggr_type"] = "multi"
        response["aggregation_id"] = self.id
        response["downtime_aggr_warn"] = self.computation_options.escalate_downtimes_as_warn
        response["use_hard_states"] = self.computation_options.use_hard_states
        response["node_visualization"] = self.aggregation_visualization
        return response

    def eval_result_node(self, node: ABCBICompiledNode) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["frozen_marker"] = node.frozen_marker
        if isinstance(node, BICompiledLeaf):
            result["type"] = 1
            result["host"] = (node.site_id, node.host_name)
            if node.service_description:
                result["service"] = node.service_description

            result["reqhosts"] = list(node.required_hosts)
            result["title"] = (
                node.host_name
                if node.service_description is None
                else f"{node.host_name} - {node.service_description}"
            )
            return result

        if isinstance(node, BICompiledRule):
            result["type"] = 2
            result["title"] = node.properties.title
            result["docu_url"] = node.properties.docu_url
            result["rule_id"] = node.id
            result["reqhosts"] = list(node.required_hosts)
            result["nodes"] = list(map(self.eval_result_node, node.nodes))
            result["rule_layout_style"] = node.node_visualization
            if node.properties.icon:
                result["icon"] = node.properties.icon
            return result

        raise NotImplementedError("Unknown node type %r" % node)

    @classmethod
    def schema(cls) -> type[BICompiledAggregationSchema]:
        return BICompiledAggregationSchema

    def serialize(self):
        return {
            "id": self.id,
            "branches": [branch.serialize() for branch in self.branches],
            "aggregation_visualization": self.aggregation_visualization,
            "computation_options": self.computation_options.serialize(),
            "groups": self.groups.serialize(),
        }

    def __str__(self):
        return f"Aggregation: {self.id}, NumBranches: {len(self.branches)}"

    def __repr__(self):
        return f"Aggregation: {self.id}, NumBranches: {len(self.branches)}"


class BICompiledAggregationSchema(Schema):
    id = ReqString()
    branches = ReqList(fields.Nested(BICompiledRuleSchema))
    aggregation_visualization = ReqNested(BIAggregationVisualizationSchema)
    computation_options = create_nested_schema_for_class(
        BIAggregationComputationOptions,
        example_config={"disabled": True},
    )

    groups = create_nested_schema_for_class(
        BIAggregationGroups,
        example_config={"names": ["groupA", "groupB"], "paths": [["path", "group", "a"]]},
    )


class BIResultSchema(OneOfSchema):
    type_field = "type"
    type_field_remove = False
    type_schemas = {
        "leaf": BICompiledLeafSchema,
        "rule": BICompiledRuleSchema,
    }

    def get_obj_type(self, obj: ABCBICompiledNode) -> str:
        return obj.kind()
