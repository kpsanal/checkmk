#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from tests.testlib.utils import cce_path, cmk_path

from tests.unit.conftest import FixPluginLegacy, FixRegister

import cmk.utils.man_pages as man_pages

from cmk.checkengine.checking import CheckPluginName

from cmk.base.plugins.server_side_calls import load_active_checks

ManPages = Mapping[str, man_pages.ManPage | None]


_IF64_MAN_PAGE = man_pages.ManPage(
    name="if64",
    path="/omd/sites/heute/share/check_mk/checkman/if64",
    title="Monitor Network Interfaces via Standard MIB Using 64-Bit Counters",
    agents=["snmp"],
    catalog=["hw", "network", "generic"],
    license="GPLv2",
    distribution="check_mk",
    description=(
        "This check does the same as {interfaces} but uses 64-bit counters from\nthe {IF-MIB}"
        " {.1.3.6.1.2.1.31.1.1.1}. This allows to correctly\nmonitor switch ports with a traffic"
        " of more then 2GB per check interval.\n\nAlso, this check can use {ifAlias} instead if"
        " ..."  # shortened for this test
    ),
    item=None,
    discovery=None,
    cluster=None,
)


def man_page_dirs_for_test(*tmp_paths: Path) -> Iterable[Path]:
    return [
        *tmp_paths,
        Path(cce_path(), "checkman"),
        Path(cmk_path(), "checkman"),
    ]


@pytest.fixture(scope="module", name="catalog")
def get_catalog() -> man_pages.ManPageCatalog:
    return man_pages.load_man_page_catalog(man_page_dirs_for_test())


@pytest.fixture(scope="module", name="all_pages")
def get_all_pages() -> ManPages:
    base_dirs = [
        Path(cce_path(), "checkman"),
        Path(cmk_path(), "checkman"),
    ]
    return {
        name: man_pages.load_man_page(name, base_dirs)
        for name in man_pages.all_man_pages(base_dirs)
    }


def test_man_page_path_only_shipped(tmp_path: Path) -> None:
    assert (
        man_pages.man_page_path("if64", man_page_dirs_for_test(tmp_path))
        == Path(cmk_path()) / "checkman" / "if64"
    )
    assert man_pages.man_page_path("not_existant", man_page_dirs_for_test(tmp_path)) is None


def test_man_page_path_both_dirs(tmp_path: Path) -> None:
    f1 = tmp_path / "file1"
    f1.write_text("x", encoding="utf-8")

    assert man_pages.man_page_path("file1", man_page_dirs_for_test(tmp_path)) == tmp_path / "file1"
    assert man_pages.man_page_path("file2", man_page_dirs_for_test(tmp_path)) is None

    f2 = tmp_path / "if"
    f2.write_text("x", encoding="utf-8")

    assert man_pages.man_page_path("if", man_page_dirs_for_test(tmp_path)) == tmp_path / "if"


def test_all_manpages_migrated(all_pages: ManPages) -> None:
    for name in all_pages:
        if name in ("check-mk-inventory", "check-mk"):
            continue
        assert CheckPluginName(name)


def test_all_man_pages(tmp_path: Path) -> None:
    (tmp_path / ".asd").write_text("", encoding="utf-8")
    (tmp_path / "asd~").write_text("", encoding="utf-8")
    (tmp_path / "if").write_text("", encoding="utf-8")

    pages = man_pages.all_man_pages(man_page_dirs_for_test(tmp_path))

    assert len(pages) > 1241
    assert ".asd" not in pages
    assert "asd~" not in pages

    assert pages["if"] == str(tmp_path / "if")
    assert pages["if64"] == "%s/checkman/if64" % cmk_path()


def test_load_all_man_pages(all_pages: ManPages) -> None:
    for _name, man_page in all_pages.items():
        assert isinstance(man_page, man_pages.ManPage)


def test_print_man_page_table(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    man_pages.print_man_page_table(man_page_dirs_for_test(tmp_path))
    out, err = capsys.readouterr()
    assert err == ""

    lines = out.split("\n")

    assert len(lines) > 1241
    assert "enterasys_powersupply" in out
    assert "IBM Websphere MQ: Channel Message Count" in out


def man_page_catalog_titles():
    assert man_pages.CATALOG_TITLES["hw"]
    assert man_pages.CATALOG_TITLES["os"]


def test_load_man_page_catalog(catalog: man_pages.ManPageCatalog) -> None:
    assert isinstance(catalog, dict)

    for path, entries in catalog.items():
        assert isinstance(path, tuple)
        assert isinstance(entries, list)

        # TODO: Test for unknown paths?

        # Test for non fallback man pages
        assert not any("Cannot parse man page" in e.title for e in entries)


def test_no_unsorted_man_pages(catalog: man_pages.ManPageCatalog) -> None:
    unsorted_page_names = [m.name for m in catalog.get(("unsorted",), [])]

    assert not unsorted_page_names


def test_manpage_files(all_pages: ManPages) -> None:
    assert len(all_pages) > 1000


def test_find_missing_manpages_passive(fix_register: FixRegister, all_pages: ManPages) -> None:
    for plugin_name in fix_register.check_plugins:
        assert str(plugin_name) in all_pages, "Manpage missing: %s" % plugin_name


def test_find_missing_manpages_active(
    fix_plugin_legacy: FixPluginLegacy, all_pages: ManPages
) -> None:
    for plugin_name in ("check_%s" % n for n in fix_plugin_legacy.active_check_info):
        assert plugin_name in all_pages, "Manpage missing: %s" % plugin_name


def test_find_missing_plugins(
    fix_register: FixRegister,
    fix_plugin_legacy: FixPluginLegacy,
    all_pages: ManPages,
) -> None:
    missing_plugins = (
        set(all_pages)
        - {str(plugin_name) for plugin_name in fix_register.check_plugins}
        - {f"check_{name}" for name in fix_plugin_legacy.active_check_info}
        - {f"check_{name}" for name in load_active_checks()[1]}
        - {
            "check-mk",
            "check-mk-inventory",
        }
    )
    assert (
        not missing_plugins
    ), f"The following manpages have no corresponding plugins: {', '.join(missing_plugins)}"


def test_cluster_check_functions_match_manpages_cluster_sections(
    fix_register: FixRegister,
    all_pages: ManPages,
) -> None:
    missing_cluster_description: set[str] = set()
    unexpected_cluster_description: set[str] = set()

    for plugin in fix_register.check_plugins.values():
        man_page = all_pages[str(plugin.name)]
        assert man_page
        has_cluster_doc = bool(man_page.cluster)
        has_cluster_func = plugin.cluster_check_function is not None
        if has_cluster_doc is not has_cluster_func:
            (
                missing_cluster_description,
                unexpected_cluster_description,
            )[
                has_cluster_doc
            ].add(str(plugin.name))

    assert not missing_cluster_description
    assert not unexpected_cluster_description


def test_no_subtree_and_entries_on_same_level(catalog: man_pages.ManPageCatalog) -> None:
    for category, entries in catalog.items():
        has_entries = bool(entries)
        has_categories = bool(man_pages._manpage_catalog_subtree_names(catalog, category))
        assert (
            has_entries != has_categories
        ), "A category must only have entries or categories, not both"


# TODO: print_man_page_browser()


def test_load_man_page_not_existing(tmp_path: Path) -> None:
    assert man_pages.load_man_page("not_existing", man_page_dirs_for_test(tmp_path)) is None


def test_print_man_page_nowiki_index(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    renderer = man_pages.NowikiManPageRenderer(_IF64_MAN_PAGE)
    index_entry = renderer.index_entry()
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""

    assert "<tr>" in index_entry
    assert "[check_if64|" in index_entry


def test_print_man_page_nowiki_content(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    renderer = man_pages.NowikiManPageRenderer(_IF64_MAN_PAGE)
    content = renderer.render()
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""

    assert content.startswith("TI:")
    assert "\nSA:" in content
    assert "License:" in content


@pytest.mark.skip("skip this until we don't need the capturing foo anymore")
def test_print_man_page(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    man_pages.ConsoleManPageRenderer(_IF64_MAN_PAGE).paint()
    out, err = capsys.readouterr()
    assert err == ""

    assert out.startswith(" if64    ")
    assert "\n License: " in out


def test_missing_catalog_entries_of_man_pages(all_pages: ManPages, tmp_path: Path) -> None:
    found_catalog_entries_from_man_pages: set[str] = set()
    for name in man_pages.all_man_pages(man_page_dirs_for_test(tmp_path)):
        man_page = all_pages[name]
        assert man_page is not None
        found_catalog_entries_from_man_pages.update(man_page.catalog)
    missing_catalog_entries = found_catalog_entries_from_man_pages - set(man_pages.CATALOG_TITLES)
    assert not missing_catalog_entries
