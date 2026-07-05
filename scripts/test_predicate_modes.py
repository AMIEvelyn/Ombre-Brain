"""End-to-end verification of the three predicate_mode behaviors against
the real facts.sqlite, using dedicated test_* predicates and a fake
subject ("qa_test") so no real fact is ever touched.

Run without arguments to execute the three tests and print PASS/FAIL.
Run with --cleanup to remove the test rows afterward.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facts_store import FactStore
from utils import load_config

TEST_SUBJECT = "qa_test"


def run_tests(store: FactStore) -> list[tuple[str, bool, str]]:
    results = []

    # --- exclusive_current: new value supersedes old, history still queryable ---
    store.upsert_predicate(
        "test_exclusive_current", mode="exclusive_current",
        notes="QA test predicate, safe to ignore/delete via --cleanup",
    )
    store.add_fact(TEST_SUBJECT, "test_exclusive_current", "v1", valid_at="2020-01-01")
    store.add_fact(TEST_SUBJECT, "test_exclusive_current", "v2", valid_at="2020-06-01")
    current = store.get_current_facts(TEST_SUBJECT, "test_exclusive_current")
    historical = [
        f for f in store.get_facts_at("2020-03-01", TEST_SUBJECT)
        if f["predicate_key"] == "test_exclusive_current"
    ]
    ok = len(current) == 1 and current[0]["object_text"] == "v2" and len(historical) == 1 and historical[0]["object_text"] == "v1"
    results.append((
        "exclusive_current：新值取代旧值，历史仍可查",
        ok,
        f"当前={[c['object_text'] for c in current]}，2020-03-01当天={[h['object_text'] for h in historical]}（期望：当前=[v2]，历史=[v1]）",
    ))

    # --- multi_current: both coexist, neither invalidates the other ---
    store.upsert_predicate(
        "test_multi_current", mode="multi_current",
        notes="QA test predicate, safe to ignore/delete via --cleanup",
    )
    store.add_fact(TEST_SUBJECT, "test_multi_current", "A")
    store.add_fact(TEST_SUBJECT, "test_multi_current", "B")
    current2 = store.get_current_facts(TEST_SUBJECT, "test_multi_current")
    ok2 = len(current2) == 2
    results.append((
        "multi_current：新旧共存，互不覆盖",
        ok2,
        f"当前={[c['object_text'] for c in current2]}（期望：包含 A 和 B 两条）",
    ))

    # --- historical_event: every event kept, none superseded ---
    store.upsert_predicate(
        "test_historical_event", mode="historical_event",
        notes="QA test predicate, safe to ignore/delete via --cleanup",
    )
    store.add_fact(TEST_SUBJECT, "test_historical_event", "event1", valid_at="2021-01-01")
    store.add_fact(TEST_SUBJECT, "test_historical_event", "event2", valid_at="2021-02-01")
    current3 = store.get_current_facts(TEST_SUBJECT, "test_historical_event")
    ok3 = len(current3) == 2
    results.append((
        "historical_event：一次性事件都保留，互不取代",
        ok3,
        f"当前={[c['object_text'] for c in current3]}（期望：包含 event1 和 event2 两条）",
    ))

    return results


def cleanup(store: FactStore) -> None:
    conn = store._connect()
    conn.execute("DELETE FROM facts WHERE subject_key = ?", (TEST_SUBJECT,))
    conn.execute("DELETE FROM predicate_registry WHERE predicate_key LIKE 'test_%'")
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="", help="Override state_dir from config.")
    parser.add_argument("--cleanup", action="store_true", help="Remove test rows instead of running tests.")
    args = parser.parse_args()

    config = load_config()
    if args.state_dir:
        config["state_dir"] = os.path.abspath(args.state_dir)
    store = FactStore(config)

    if args.cleanup:
        cleanup(store)
        print("已清理 test_* predicate 和 qa_test 主体下的测试数据。")
        return

    results = run_tests(store)
    print(f"db: {store.db_path}\n")
    all_pass = True
    for label, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"[{status}] {label}\n       {detail}")
    print("\n" + ("全部通过 ALL TESTS PASSED" if all_pass else "有测试失败，见上面 FAIL 那几行"))
    print("\n测完想清理测试数据的话，加 --cleanup 参数再跑一次。")


if __name__ == "__main__":
    main()
