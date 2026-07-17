import copy
import json
import sys
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

from parse_and_evaluate import score_answer_rules, validate_dataset  # noqa: E402


def load_data():
    data_path = PACKAGE_DIR / "finance_memory_single_session_timeline.json"
    return json.loads(data_path.read_text(encoding="utf-8"))


class ScoringRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_data()
        cls.queries = {query["query_id"]: query for query in cls.data["evaluation_queries"]}

    def assert_rule_result(self, qid, answer, *, hard_failure, forbidden):
        result = score_answer_rules(self.queries[qid], answer)
        self.assertEqual(bool(result["hard_failure_matches"]), hard_failure, msg=result)
        self.assertEqual(bool(result["forbidden_matches"]), forbidden, msg=result)

    def assert_clean(self, qid, answer):
        self.assert_rule_result(qid, answer, hard_failure=False, forbidden=False)

    def assert_hard_and_forbidden(self, qid, answer):
        self.assert_rule_result(qid, answer, hard_failure=True, forbidden=True)

    def test_q001_initial_risk_value(self):
        cases_ok = [
            "当时在S001里明确说的是10%，达到这个亏损比例会明显焦虑。",
            "当时是10%，不是后来的5%。",
            "5%属于后续会话，本次历史值是10%。",
            "这次应按10%理解，5%不是本次S001里的风险值。",
            "这题信息不足，我无法确认具体比例。",
        ]
        for answer in cases_ok:
            with self.subTest(answer=answer):
                self.assert_clean("Q001", answer)
        for answer in [
            "本次S001的风险值是5%，亏损到5%会明显焦虑。",
            "本次不是10%，而是5%。",
            "S001里的风险值就是5%。",
        ]:
            with self.subTest(answer=answer):
                self.assert_hard_and_forbidden("Q001", answer)

    def test_q004_original_down_payment_date(self):
        cases_ok = [
            "本次S002会话是2027年4月，首付约10万元并单独管理。",
            "本次会话是2027年4月，不是后来更新的2026年11月。",
            "2026年11月属于后续会话，S002里的日期是2027年4月。",
            "S002本次首付日期不是2026年11月，而是2027年4月。",
            "我只能确认需要单独管理，但日期没有足够信息。",
        ]
        for answer in cases_ok:
            with self.subTest(answer=answer):
                self.assert_clean("Q004", answer)
        for answer in [
            "S002本次购房会话的首付日期是2026年11月。",
            "本次不是2027年4月，而是2026年11月。",
            "S002首付日期就是2026年11月。",
        ]:
            with self.subTest(answer=answer):
                self.assert_hard_and_forbidden("Q004", answer)

    def test_q005_latest_down_payment_date(self):
        cases_ok = [
            "当前是2026年11月，2027年4月已经失效。",
            "当前日期是2026年11月，不能再把2027年4月作为当前日期。",
            "2027年4月是旧日期，最新计划应按2026年11月处理。",
            "不能再把2027年4月作为当前日期，当前应以2026年11月为准。",
            "我无法从这段回答里确认当前日期。",
        ]
        for answer in cases_ok:
            with self.subTest(answer=answer):
                self.assert_clean("Q005", answer)
        for answer in [
            "2027年4月仍然是当前有效的首付日期。",
            "当前不是2026年11月，而是2027年4月。",
            "2027年4月不是旧日期，仍然是当前有效日期。",
            "当前仍按2027年4月执行。",
        ]:
            with self.subTest(answer=answer):
                self.assert_hard_and_forbidden("Q005", answer)

    def test_q006_latest_loss_limit_and_sector_fund(self):
        cases_ok = [
            "当前最多接受5%的本金亏损，不建议把首付主要投入新能源行业基金。",
            "当前不是10%，而是5%；10%是旧值，不能继续作为当前上限。",
            "10%是旧值，当前应按5%处理，所以不适合大比例买新能源。",
            "不太建议把首付大部分投入新能源基金。",
            "不是很建议把首付主要投入新能源基金。",
            "建议不要把首付大部分投入新能源基金。",
            "不适合把首付大部分投入新能源基金。",
            "用户只是问是否可以把首付大部分投入新能源基金，这里不能当作肯定建议。",
            "我只能说明需要更多信息，不能确认风险上限。",
        ]
        for answer in cases_ok:
            with self.subTest(answer=answer):
                self.assert_clean("Q006", answer)
        for answer in [
            "当前有效风险上限是10%，可以按10%来安排。",
            "当前不是5%，而是10%。",
        ]:
            with self.subTest(answer=answer):
                self.assert_hard_and_forbidden("Q006", answer)
        for answer in [
            "建议把首付大部分投入新能源行业基金。",
            "适合把主要首付资金投入新能源行业基金。",
            "可以将全部首付投入新能源基金。",
            "应当把大部分首付款买新能源行业基金。",
            "问题不大，可以把首付大部分投入新能源基金。",
        ]:
            with self.subTest(answer=answer):
                self.assert_rule_result("Q006", answer, hard_failure=True, forbidden=False)

    def test_unknown_semantic_rule_is_reported_by_validation(self):
        data = copy.deepcopy(self.data)
        query = next(item for item in data["evaluation_queries"] if item["query_id"] == "Q001")
        query["answer_checks"]["hard_failure_patterns"] = ["semantic:q001_typo_rule"]
        errors = validate_dataset(data)
        joined = "\n".join(errors)
        self.assertIn("Turn/Query ID: Q001", joined)
        self.assertIn("相关字段: hard_failure_patterns", joined)
        self.assertIn("未知规则名称: semantic:q001_typo_rule", joined)

    def test_unknown_semantic_rule_raises_in_scoring(self):
        query = copy.deepcopy(self.queries["Q001"])
        query["answer_checks"]["forbidden_patterns"] = ["semantic:q001_typo_rule"]
        with self.assertRaisesRegex(ValueError, "未知semantic规则: semantic:q001_typo_rule"):
            score_answer_rules(query, "当时是10%。")


if __name__ == "__main__":
    unittest.main()
