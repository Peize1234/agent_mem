_PROFILE_EXTRACTION_RULE = (
    "当本轮用户消息包含与该画像字段直接相关、含义明确且能够可靠归一化的信息时，可以提取。"
    "应结合 current_profile 判断是新增、替换、追加、局部修改还是删除。"
    "不得提取当前财报数据、经营指标具体数值、预算或预测具体数值以及当前分析结果，"
    "也不得根据用户身份、职位或组织归属推断或扩大数据权限。"
)


PREDEFINED_PROFILE_ATTRIBUTES = [
    {
        "attribute_key": "analysis_role",
        "attribute_name": "企业分析角色",
        "attribute_category": "user_context",
        "description": (
            "用户直接描述岗位、职责或角色时可以提取；不能仅根据一次专业问题推断岗位。financial_analyst 表示财务分析师，"
            "management_accountant "
            "表示管理会计，fp_and_a 表示财务规划与分析，business_controller 表示业务财务或财务控制，"
            "finance_manager 表示财务管理者，executive 表示企业管理层，auditor 表示审计人员，other 表示其他角色。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string",
        "value_schema": {
            "type": "string",
            "enum": [
                "financial_analyst",
                "management_accountant",
                "fp_and_a",
                "business_controller",
                "finance_manager",
                "executive",
                "auditor",
                "other",
            ],
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "financial_analysis_expertise_level",
        "attribute_name": "财务分析专业水平",
        "attribute_category": "user_context",
        "description": (
            "根据用户对企业财务分析专业水平的自我描述提取；不能仅根据术语使用或问题复杂度推断。beginner 表示入门，"
            "intermediate 表示中级，advanced 表示高级，expert 表示专家。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string",
        "value_schema": {
            "type": "string",
            "enum": ["beginner", "intermediate", "advanced", "expert"],
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "default_report_audience",
        "attribute_name": "默认报告受众",
        "attribute_category": "user_context",
        "description": (
            "用户明确说明报告受众时可以提取。finance_team 表示财务团队，business_management 表示业务管理层，"
            "senior_management 表示高级管理层，board 表示董事会，investors 表示投资者，auditors 表示审计人员，"
            "regulators 表示监管机构，cross_functional 表示跨职能团队。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string",
        "value_schema": {
            "type": "string",
            "enum": [
                "finance_team",
                "business_management",
                "senior_management",
                "board",
                "investors",
                "auditors",
                "regulators",
                "cross_functional",
            ],
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "default_analysis_scope",
        "attribute_name": "默认分析范围",
        "attribute_category": "analysis_scope",
        "description": (
            "本轮消息中明确出现组织、法人主体、业务单元、部门、区域或产品线时可以提取为分析范围；"
            "分析范围不能用于推断或扩大数据访问权限。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "organization": {"type": ["string", "null"]},
                "legal_entities": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "business_units": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "departments": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "regions": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "product_lines": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "primary_analysis_tasks",
        "attribute_name": "主要分析任务",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确提出的分析任务可以提取。financial_statement_analysis 表示财务报表分析，"
            "management_reporting 表示管理报告，budget_variance_analysis 表示预算差异分析，forecasting_analysis "
            "表示预测分析，profitability_analysis 表示盈利能力分析，cash_flow_analysis 表示现金流分析，"
            "working_capital_analysis 表示营运资金分析，cost_expense_analysis 表示成本费用分析，segment_analysis "
            "表示分部分析，scenario_analysis 表示情景分析，risk_monitoring 表示风险监控，audit_support 表示审计支持，"
            "board_reporting 表示董事会报告。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "financial_statement_analysis",
                    "management_reporting",
                    "budget_variance_analysis",
                    "forecasting_analysis",
                    "profitability_analysis",
                    "cash_flow_analysis",
                    "working_capital_analysis",
                    "cost_expense_analysis",
                    "segment_analysis",
                    "scenario_analysis",
                    "risk_monitoring",
                    "audit_support",
                    "board_reporting",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "analysis_focus_areas",
        "attribute_name": "分析关注领域",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确关注的财务或经营领域可以提取。revenue_growth 表示收入增长，profitability 表示盈利能力，cash_flow "
            "表示现金流，working_capital 表示营运资金，cost_efficiency 表示成本效率，budget_execution 表示预算执行，"
            "forecast_accuracy 表示预测准确性，operational_efficiency 表示运营效率，financial_position 表示财务状况，"
            "capital_structure 表示资本结构，tax 表示税务，compliance 表示合规。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "revenue_growth",
                    "profitability",
                    "cash_flow",
                    "working_capital",
                    "cost_efficiency",
                    "budget_execution",
                    "forecast_accuracy",
                    "operational_efficiency",
                    "financial_position",
                    "capital_structure",
                    "tax",
                    "compliance",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "preferred_kpis",
        "attribute_name": "偏好关键指标",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确要求重点查看的关键指标可以提取，使用普通字符串列表，例如毛利率、经营现金流和应收账款；"
            "不得保存指标具体数值。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "preferred_comparison_baselines",
        "attribute_name": "偏好比较基准",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确要求的比较方式可以提取。budget 表示预算，forecast 表示预测，prior_period 表示上期或环比，"
            "year_over_year "
            "表示同比，industry_benchmark 表示行业基准，strategic_target 表示战略目标，rolling_average 表示滚动均值。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "budget",
                    "forecast",
                    "prior_period",
                    "year_over_year",
                    "industry_benchmark",
                    "strategic_target",
                    "rolling_average",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "preferred_time_granularity",
        "attribute_name": "偏好时间粒度",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确提出的分析时间粒度可以提取。daily 表示日度，weekly 表示周度，monthly 表示月度，quarterly "
            "表示季度，annual 表示年度，year_to_date 表示年初至今，rolling_12_months 表示滚动十二个月。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "daily",
                    "weekly",
                    "monthly",
                    "quarterly",
                    "annual",
                    "year_to_date",
                    "rolling_12_months",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "risk_focus_areas",
        "attribute_name": "风险关注领域",
        "attribute_category": "analysis_preference",
        "description": (
            "用户本轮明确关注的风险类别可以提取。liquidity_risk 表示流动性风险，credit_risk 表示信用风险，market_risk "
            "表示市场风险，operational_risk 表示运营风险，compliance_risk 表示合规风险，tax_risk 表示税务风险，"
            "fraud_risk 表示舞弊风险，going_concern_risk 表示持续经营风险，concentration_risk 表示集中度风险，"
            "forecast_risk 表示预测偏差风险。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "liquidity_risk",
                    "credit_risk",
                    "market_risk",
                    "operational_risk",
                    "compliance_risk",
                    "tax_risk",
                    "fraud_risk",
                    "going_concern_risk",
                    "concentration_risk",
                    "forecast_risk",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "decision_contexts",
        "attribute_name": "决策应用场景",
        "attribute_category": "analysis_preference",
        "description": (
            "用户说明分析用于何种决策时可以提取。budgeting 表示预算编制，forecasting 表示滚动预测，performance_review "
            "表示绩效复盘，resource_allocation 表示资源配置，pricing 表示定价，cost_control 表示成本控制，"
            "investment_decision 表示投资决策，financing_decision 表示融资决策，risk_management 表示风险管理，"
            "board_decision 表示董事会决策，audit_response 表示审计应对。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "string_list",
        "value_schema": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "budgeting",
                    "forecasting",
                    "performance_review",
                    "resource_allocation",
                    "pricing",
                    "cost_control",
                    "investment_decision",
                    "financing_decision",
                    "risk_management",
                    "board_decision",
                    "audit_response",
                ],
            },
            "uniqueItems": True,
        },
        "merge_policy": "append_unique",
    },
    {
        "attribute_key": "default_analysis_horizon",
        "attribute_name": "默认分析期间",
        "attribute_category": "analysis_preference",
        "description": (
            "用户明确提出回溯或预测期间时可以提取。period_unit 的 month、quarter、year 分别表示月、季度、年；"
            "lookback_periods 和 forecast_periods 分别表示回溯和预测的期数，horizon_type 的 historical、forecast、"
            "mixed 分别表示历史、预测和混合分析。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "horizon_type": {
                    "type": ["string", "null"],
                    "enum": ["historical", "forecast", "mixed", None],
                },
                "lookback_periods": {"type": ["integer", "null"], "minimum": 1},
                "forecast_periods": {"type": ["integer", "null"], "minimum": 1},
                "period_unit": {
                    "type": ["string", "null"],
                    "enum": ["month", "quarter", "year", None],
                },
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "materiality_threshold",
        "attribute_name": "重要性阈值偏好",
        "attribute_category": "analysis_preference",
        "description": (
            "用户明确提出用于筛选重大差异的重要性阈值时可以提取。basis 的 amount、ratio、both 分别表示按金额、"
            "比例或两者判断；"
            "amount 表示金额阈值，ratio 表示比例阈值（例如 5% 保存为 0.05），currency 表示币种编码。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "basis": {
                    "type": ["string", "null"],
                    "enum": ["amount", "ratio", "both", None],
                },
                "amount": {"type": ["number", "null"], "minimum": 0},
                "ratio": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "maximum": 1,
                },
                "currency": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "anomaly_detection_preference",
        "attribute_name": "异常识别偏好",
        "attribute_category": "analysis_preference",
        "description": (
            "用户明确提出异常识别方式时可以提取。sensitivity 的 low、medium、high 分别表示低、中、高敏感度；"
            "detect_outliers、detect_trends 和 detect_seasonality 分别控制离群、趋势和季节性异常识别。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "sensitivity": {
                    "type": ["string", "null"],
                    "enum": ["low", "medium", "high", None],
                },
                "detect_outliers": {"type": ["boolean", "null"]},
                "detect_trends": {"type": ["boolean", "null"]},
                "detect_seasonality": {"type": ["boolean", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "default_reporting_basis",
        "attribute_name": "默认报告口径",
        "attribute_category": "reporting_preference",
        "description": (
            "用户明确提出会计准则、合并范围或汇率口径时可以提取。accounting_standard 的 prc_gaap、ifrs、us_gaap、"
            "local_gaap、management_basis "
            "分别表示中国企业会计准则、国际财务报告准则、美国会计准则、当地准则和管理口径；consolidation_scope 的 "
            "consolidated、standalone、both 分别表示合并、单体和两者；currency_basis 的 reported、constant_currency "
            "分别表示报告汇率和固定汇率口径。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "accounting_standard": {
                    "type": ["string", "null"],
                    "enum": ["prc_gaap", "ifrs", "us_gaap", "local_gaap", "management_basis", None],
                },
                "consolidation_scope": {
                    "type": ["string", "null"],
                    "enum": ["consolidated", "standalone", "both", None],
                },
                "currency_basis": {
                    "type": ["string", "null"],
                    "enum": ["reported", "constant_currency", None],
                },
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "currency_display_preference",
        "attribute_name": "币种展示偏好",
        "attribute_category": "reporting_preference",
        "description": (
            "用户明确提出金额展示方式时可以提取。currency 表示币种编码；unit 的 unit、thousand、ten_thousand、million、"
            "hundred_million、billion 分别表示元、千元、万元、百万元、亿元、十亿元；decimal_places 表示小数位数，"
            "show_currency_symbol 表示是否显示币种符号。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "currency": {"type": ["string", "null"]},
                "unit": {
                    "type": ["string", "null"],
                    "enum": [
                        "unit",
                        "thousand",
                        "ten_thousand",
                        "million",
                        "hundred_million",
                        "billion",
                        None,
                    ],
                },
                "decimal_places": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 4,
                },
                "show_currency_symbol": {"type": ["boolean", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "evidence_requirements",
        "attribute_name": "证据要求",
        "attribute_category": "reporting_preference",
        "description": (
            "用户明确提出分析证据要求时可以提取。evidence_level 的 summary、standard、detailed 分别表示摘要、标准和详细"
            "证据；require_source_citations、require_reconciliation、require_calculation_details 分别表示是否需要来源引用、"
            "勾稽核对和计算过程。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "evidence_level": {
                    "type": ["string", "null"],
                    "enum": ["summary", "standard", "detailed", None],
                },
                "require_source_citations": {"type": ["boolean", "null"]},
                "require_reconciliation": {"type": ["boolean", "null"]},
                "require_calculation_details": {"type": ["boolean", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "recommendation_preferences",
        "attribute_name": "建议呈现偏好",
        "attribute_category": "reporting_preference",
        "description": (
            "用户明确提出管理建议的呈现方式时可以提取。recommendation_depth 的 strategic、operational、both 分别表示战略、"
            "执行和两者兼顾；include_recommendations、include_owner、include_deadline、include_impact_estimate 分别表示"
            "是否需要建议、责任人、期限和影响估算。" + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "include_recommendations": {"type": ["boolean", "null"]},
                "recommendation_depth": {
                    "type": ["string", "null"],
                    "enum": ["strategic", "operational", "both", None],
                },
                "include_owner": {"type": ["boolean", "null"]},
                "include_deadline": {"type": ["boolean", "null"]},
                "include_impact_estimate": {"type": ["boolean", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
    {
        "attribute_key": "response_preferences",
        "attribute_name": "回答与报告偏好",
        "attribute_category": "reporting_preference",
        "description": (
            "用户本轮明确提出表格、图表、结论前置、语言、格式或语气等要求时可以提取。language 表示语言；"
            "detail_level 的 concise、standard、detailed 分别表示简洁、标准、详细；output_format 的 narrative、"
            "bullet_points、table、slides 分别表示叙述、要点、表格、演示文稿结构；tone 的 professional、executive、"
            "technical 分别表示专业、管理层和技术语气；其余布尔字段控制结论前置、表格、图表、执行摘要和行动项。"
            + _PROFILE_EXTRACTION_RULE
        ),
        "value_type": "object",
        "value_schema": {
            "type": "object",
            "properties": {
                "language": {"type": ["string", "null"]},
                "detail_level": {
                    "type": ["string", "null"],
                    "enum": ["concise", "standard", "detailed", None],
                },
                "output_format": {
                    "type": ["string", "null"],
                    "enum": ["narrative", "bullet_points", "table", "slides", None],
                },
                "tone": {
                    "type": ["string", "null"],
                    "enum": ["professional", "executive", "technical", None],
                },
                "conclusion_first": {"type": ["boolean", "null"]},
                "include_tables": {"type": ["boolean", "null"]},
                "include_charts": {"type": ["boolean", "null"]},
                "include_executive_summary": {"type": ["boolean", "null"]},
                "include_action_items": {"type": ["boolean", "null"]},
            },
            "additionalProperties": False,
        },
        "merge_policy": "replace",
    },
]
