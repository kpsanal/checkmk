#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Example output from agent:
# 0 Service_FOO V=1 This Check is OK
# 1 Bar_Service - This is WARNING and has no performance data
# 2 NotGood V=120;50;100;0;1000 A critical check
# P Some_other_Service value1=10;30;50|value2=20;10:20;0:50;0;100 Result is computed from two values
# P This_is_OK foo=18;20;50
# P Some_yet_other_Service temp=40;30;50|humidity=28;50:100;0:50;0;100
# P Has-no-var - This has no variable
# P No-Text hirn=-8;-20
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Union,
    Iterable,
    Sequence,
)

import shlex
import time
import six

from .agent_based_api.v1.type_defs import (
    DiscoveryResult,
    StringTable,
)
from .agent_based_api.v1 import (
    Result,
    Metric,
    Service,
    State,
    check_levels,
    register,
    render,
)
from .agent_based_api.v1.clusterize import make_node_notice_results

# we don't have IgnoreResults and thus don't want to handle them
LocalCheckResult = Iterable[Union[Metric, Result]]
Levels = Optional[Tuple[float, float]]


class Perfdata(NamedTuple):
    name: str
    value: float
    levels_upper: Levels
    levels_lower: Levels
    boundaries: Optional[Tuple[Optional[float], Optional[float]]]


class LocalResult(NamedTuple):
    cached: Optional[Tuple[float, float, float]]
    item: str
    state: State
    apply_levels: bool
    text: str
    perfdata: Iterable[Perfdata]


class LocalError(NamedTuple):
    output: str
    reason: str


class LocalSection(NamedTuple):
    errors: List[LocalError]
    data: Mapping[str, LocalResult]


def float_ignore_uom(value: str) -> float:
    '''16MB -> 16.0'''
    while value:
        try:
            return float(value)
        except ValueError:
            value = value[:-1]
    return 0.0


def _try_convert_to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def _parse_cache(line: str, now: float) -> Tuple[Optional[Tuple[float, float, float]], str]:
    """add cache info, if found"""
    if not line or not line[0].startswith("cached("):
        return None, line

    cache_raw, stripped_line = line[0], line[1:]
    creation_time, interval = (float(v) for v in cache_raw[7:-1].split(',', 1))
    age = now - creation_time

    # make sure max(..) will give the oldest/most outdated case
    return (age, 100.0 * age / interval, interval), stripped_line


def _is_valid_line(line: str) -> bool:
    return len(line) >= 4 or (len(line) == 3 and line[0] == 'P')


def _get_violation_reason(line: Sequence[str]) -> str:
    if len(line) == 0:
        return "Received empty line. Did any of your local checks returned a superfluous newline character?"
    if len(line) < 4 and not (len(line) == 3 and line[0] == 'P'):
        return ("Received wrong format of local check output. "
                "Please read the documentation regarding the correct format: "
                "https://docs.checkmk.com/2.0.0/de/localchecks.html ")
    return ""


def _sanitize_state(raw_state: str) -> Tuple[Union[int, str], str]:
    state_mapping: Mapping[str, Tuple[Union[int, str], str]] = {
        "0": (0, ""),
        "1": (1, ""),
        "2": (2, ""),
        "3": (3, ""),
        "P": ("P", ""),
    }
    return state_mapping.get(raw_state, (3, f"Invalid plugin status {raw_state}."))


def _parse_perfentry(entry: str) -> Perfdata:
    '''Parse single perfdata entry, syntax is:
        NAME=VALUE[;[[WARN_LOWER:]WARN_UPPER][;[[CRIT_LOWER:]CRIT_UPPER][;[MIN][;MAX]]]]

    see https://docs.checkmk.com/latest/de/localchecks.html
    '''
    entry = entry.rstrip(";")
    name, raw_list = entry.split('=', 1)
    raw = raw_list.split(";")
    value = float_ignore_uom(raw[0])

    # create a check_levels compatible levels quadruple
    levels: List[Optional[float]] = [None] * 4
    if len(raw) >= 2:
        warn = raw[1].split(':', 1)
        levels[0] = _try_convert_to_float(warn[-1])
        if len(warn) > 1:
            levels[2] = _try_convert_to_float(warn[0])
    if len(raw) >= 3:
        crit = raw[2].split(':', 1)
        levels[1] = _try_convert_to_float(crit[-1])
        if len(crit) > 1:
            levels[3] = _try_convert_to_float(crit[0])

    # the critical level can be set alone, in this case warning will be equal to critical
    if levels[0] is None and levels[1] is not None:
        levels[0] = levels[1]
    if levels[2] is None and levels[3] is not None:
        levels[2] = levels[3]

    # check_levels won't handle crit=None, if warn is present.
    if levels[0] is not None and levels[1] is None:
        levels[1] = float('inf')
    if levels[2] is not None and levels[3] is None:
        levels[3] = float('-inf')

    def optional_tuple(warn: Optional[float], crit: Optional[float]) -> Levels:
        assert (warn is None) == (crit is None)
        if warn is not None and crit is not None:
            return warn, crit
        return None

    return Perfdata(
        name,
        value,
        levels_upper=optional_tuple(levels[0], levels[1]),
        levels_lower=optional_tuple(levels[2], levels[3]),
        boundaries=(float(raw[3]) if len(raw) >= 4 else None,
                    float(raw[4]) if len(raw) >= 5 else None),
    )


def _parse_perftxt(string: str) -> Tuple[Iterable[Perfdata], str]:
    if string == '-':
        return [], ""

    perfdata = []
    msg = []
    for entry in string.split('|'):
        try:
            perfdata.append(_parse_perfentry(entry))
        except (ValueError, IndexError):
            msg.append(entry)
    if msg:
        return perfdata, "Invalid performance data: %r. " % "|".join(msg)
    return perfdata, ""


def parse_local(string_table: StringTable) -> LocalSection:
    # Wrap pure counterpart
    return parse_local_pure(string_table, time.time())


def parse_local_pure(string_table: Iterable[Sequence[str]], now: float) -> LocalSection:
    """
    >>> parse_local_pure([['0 "Service Name" - arbitrary info text']], 1617883538).data
    {'Service Name': LocalResult(cached=None, item='Service Name', state=<State.OK: 0>, apply_levels=False, text='arbitrary info text', perfdata=[])}
    >>> parse_local_pure([['cached(1617883538,1617883538) 0 "Service Name" - arbitrary info text']], 1617883538).data
    {'Service Name': LocalResult(cached=(0.0, 0.0, 1617883538.0), item='Service Name', state=<State.OK: 0>, apply_levels=False, text='arbitrary info text', perfdata=[])}
    """
    errors = []
    data = {}
    for line in string_table:
        # allows blank characters in service description
        if len(line) == 1:
            # from agent version 1.7, local section with ":sep(0)"
            # In python2 shlex uses cStringIO (if available), which is not able to deal with unicode
            # strings *urgs* (See https://docs.python.org/2/library/stringio.html#module-cStringIO).
            # To workaround this, we encode/and decode for shlex.
            stripped_line = [six.ensure_text(s) for s in shlex.split(six.ensure_str(line[0]))]
        else:
            stripped_line = line  # type: ignore

        cached, stripped_line = _parse_cache(stripped_line, now)  # type: ignore
        if not _is_valid_line(stripped_line):  # type: ignore[arg-type]
            # just pass on the line and reason, to report the offending ouput
            errors.append(
                LocalError(
                    output=" ".join(stripped_line),
                    reason=_get_violation_reason(stripped_line),
                ))
            continue

        raw_state, state_msg = _sanitize_state(stripped_line[0])
        item = stripped_line[1]
        perfdata, perf_msg = _parse_perftxt(stripped_line[2])
        # convert escaped newline chars
        # (will be converted back later individually for the different cores)
        text = " ".join(stripped_line[3:]).replace("\\n", "\n")
        if state_msg or perf_msg:
            raw_state = 3
            text = "%s%sOutput is: %s" % (state_msg, perf_msg, text)
        data[item] = LocalResult(
            cached=cached,
            item=item,
            state=State(raw_state) if raw_state != 'P' else State.OK,
            apply_levels=raw_state == 'P',
            text=text,
            perfdata=perfdata,
        )

    return LocalSection(errors=errors, data=data)


register.agent_section(
    name="local",
    parse_function=parse_local,
)

_STATE_MARKERS = {
    State.OK: "",
    State.WARN: "(!)",
    State.UNKNOWN: "(?)",
    State.CRIT: "(!!)",
}


# Compute state according to warn/crit levels contained in the
# performance data.
def _local_make_metrics(local_result: LocalResult) -> LocalCheckResult:
    for entry in local_result.perfdata:
        yield from check_levels(
            entry.value,
            # check_levels does not like levels like (23, None), but it does deal with it.
            levels_upper=entry.levels_upper if local_result.apply_levels else None,
            levels_lower=entry.levels_lower if local_result.apply_levels else None,
            metric_name=entry.name,
            label=_labelify(entry.name),
            boundaries=entry.boundaries,
        )


def _labelify(word: str) -> str:
    """
        >>> _labelify("weekIncidence")
        'Week incidence'
        >>> _labelify("casesPer100k")
        'Cases per 100 k'
        >>> _labelify("WHOrecommendation4")
        'WHO recommendation 4'
        >>> _labelify("zombie_apocalypse")
        'Zombie apocalypse'

    """
    label = ''.join("%s%s" % (
        this if prev.isupper() else this.lower(),
        ' ' if (  #
            prev.isupper() and this.isupper() and nxt.islower() or  #
            this.islower() and nxt.isupper() or  #
            this.isdigit() is not nxt.isdigit()  #
        ) else '',
    ) for prev, this, nxt in zip(' ' + word, word, word[1:] + ' '))
    return (label[0].upper() + label[1:].replace('_', ' ')).strip()


def discover_local(section: LocalSection) -> DiscoveryResult:
    if section.errors:
        output = section.errors[0].output
        reason = section.errors[0].reason
        raise ValueError(("Invalid line in agent section <<<local>>>. "
                          "Reason: %s First offending line: \"%s\"" % (reason, output)))

    for key in section.data:
        yield Service(item=key)


def check_local(item: str, params: Mapping[str, Any], section: LocalSection) -> LocalCheckResult:
    local_result = section.data.get(item)
    if local_result is None:
        return

    try:
        summary, details = local_result.text.split("\n", 1)
    except ValueError:
        summary, details = local_result.text, ""

    if local_result.text:
        yield Result(
            state=local_result.state,
            summary=summary,
            details=details if details else None,
        )
    yield from _local_make_metrics(local_result)

    if local_result.cached is not None:
        # We try to mimic the behaviour of cached agent sections.
        # Problem here: We need this info on a per-service basis, so we cannot use the section header.
        # Solution: Just add an informative message with the same wording as in cmk/gui/plugins/views/utils.py
        infotext = "Cache generated %s ago, Cache interval: %s, Elapsed cache lifespan: %s" % (
            render.timespan(local_result.cached[0]),
            render.timespan(local_result.cached[2]),
            render.percent(local_result.cached[1]),
        )
        yield Result(state=State.OK, summary=infotext)


def cluster_check_local(
    item: str,
    params: Mapping[str, Any],
    section: Mapping[str, LocalSection],
) -> LocalCheckResult:

    # collect the result instances and yield the rest
    results_by_node: Dict[str, LocalCheckResult] = {}
    for node, node_section in section.items():
        node_results = list(check_local(item, {}, node_section))
        if node_results:
            results_by_node[node] = node_results
    if not results_by_node:
        return

    if params is None or params.get("outcome_on_cluster", "worst") == "worst":
        yield from _aggregate_worst(results_by_node)
    else:
        yield from _aggregate_best(results_by_node)


def _aggregate_worst(node_results: Dict[str, LocalCheckResult]) -> LocalCheckResult:
    node_states: Dict[State, str] = {}
    for node_name, results in node_results.items():
        node_states.setdefault(
            State.worst(*(r.state for r in results if isinstance(r, Result))),
            node_name,
        )

    global_worst_state = State.worst(*node_states)
    worst_node = node_states[global_worst_state]

    for node_result in node_results[worst_node]:
        if isinstance(node_result, Result):
            yield Result(
                state=node_result.state,
                summary="[%s]: %s" % (worst_node, node_result.summary),
                details="[%s]: %s" % (worst_node, node_result.details),
            )
        else:  # Metric
            yield node_result

    for node, results in node_results.items():
        if node != worst_node:
            yield from make_node_notice_results(node, results)


def _aggregate_best(node_results: Dict[str, LocalCheckResult]) -> LocalCheckResult:
    node_states: Dict[State, str] = {}
    for node_name, results in node_results.items():
        node_states.setdefault(
            State.worst(*(r.state for r in results if isinstance(r, Result))),
            node_name,
        )

    global_best_state = State.best(*node_states)
    best_node = node_states[global_best_state]

    for node_result in node_results[best_node]:
        if isinstance(node_result, Result):
            yield Result(
                state=node_result.state,
                summary="[%s]: %s" % (best_node, node_result.summary),
                details="[%s]: %s" % (best_node, node_result.details),
            )
        else:  # Metric
            yield node_result

    for node, results in node_results.items():
        if node != best_node:
            yield from make_node_notice_results(node, results, force_ok=True)


register.check_plugin(
    name="local",
    service_name="%s",
    discovery_function=discover_local,
    check_default_parameters={},
    check_ruleset_name="local",
    check_function=check_local,
    cluster_check_function=cluster_check_local,
)
