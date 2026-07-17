# FinanceMemory Single Session 数据检查报告

- Benchmark: FinanceMemory-SingleSession-CN v5.1.0
- Sessions: 5
- Turns: 80
- Memories: 21
- Queries: 9
- Validation: PASS

## Q001 / S001

- Question: 只根据这次投资画像会话，我明确说本金亏损到什么比例会明显焦虑？
- Topic: initial_loss_tolerance
- Target Memory IDs: ['M003(active)']
- Relevant Turns: ["T007(user, memory_ids=['M003'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T021 user: 我看到有些基金页面同时显示净值和估值，这两个数字是不是一回事，平时要不要盯着估值下单？
  - T022 assistant: 净值是基金公司按持仓和规则计算后的正式结果，估值只是盘中或第三方的近似参考。普通投资者不必围绕估值频繁交易，更应关注产品是否符合自己的长期规则。
  - T023 user: 如果定投扣款日正好遇到节假日或市场休市，是不是会导致计划失效，需要我手动补操作？
  - T024 assistant: 不会因为遇到休市就让计划失效。一般会顺延到下一个交易日或按平台规则处理，你只需要确认扣款账户余额充足，并定期查看是否连续扣款成功。

## Q002 / S001

- Question: 不依赖我的个人画像，长期债券基金为什么在利率上升时通常波动更明显？
- Topic: bond_duration_and_interest_rate
- Target Memory IDs: ['-']
- Relevant Turns: ['-']
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T021 user: 我看到有些基金页面同时显示净值和估值，这两个数字是不是一回事，平时要不要盯着估值下单？
  - T022 assistant: 净值是基金公司按持仓和规则计算后的正式结果，估值只是盘中或第三方的近似参考。普通投资者不必围绕估值频繁交易，更应关注产品是否符合自己的长期规则。
  - T023 user: 如果定投扣款日正好遇到节假日或市场休市，是不是会导致计划失效，需要我手动补操作？
  - T024 assistant: 不会因为遇到休市就让计划失效。一般会顺延到下一个交易日或按平台规则处理，你只需要确认扣款账户余额充足，并定期查看是否连续扣款成功。

## Q003 / S001

- Question: 只根据这次会话，我的长期核心产品偏好是什么，又明确排除了哪些工具？
- Topic: long_term_product_preference
- Target Memory IDs: ['M005(active)', 'M006(active)', 'M007(active)']
- Relevant Turns: ["T011(user, memory_ids=['M005'])", "T013(user, memory_ids=['M006'])", "T015(user, memory_ids=['M007'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T021 user: 我看到有些基金页面同时显示净值和估值，这两个数字是不是一回事，平时要不要盯着估值下单？
  - T022 assistant: 净值是基金公司按持仓和规则计算后的正式结果，估值只是盘中或第三方的近似参考。普通投资者不必围绕估值频繁交易，更应关注产品是否符合自己的长期规则。
  - T023 user: 如果定投扣款日正好遇到节假日或市场休市，是不是会导致计划失效，需要我手动补操作？
  - T024 assistant: 不会因为遇到休市就让计划失效。一般会顺延到下一个交易日或按平台规则处理，你只需要确认扣款账户余额充足，并定期查看是否连续扣款成功。

## Q004 / S002

- Question: 只根据本次购房会话，首付计划的日期、金额和账户安排分别是什么？
- Topic: down_payment_plan_summary
- Target Memory IDs: ['M011(active)', 'M012(active)', 'M013(active)']
- Relevant Turns: ["T025(user, memory_ids=['M011'])", "T027(user, memory_ids=['M012'])", "T028(assistant, memory_ids=['M013'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T031 user: 我还想把家庭预算做得清楚一点，日常开销和阶段性大额支出应该放在同一张表里吗？
  - T032 assistant: 可以放在同一张表，但最好分栏管理。日常开销看月度稳定性，阶段性支出看发生时间和资金来源；这样不会把经常性消费和一次性支出混在一起。
  - T033 user: 如果之后我更新家庭预算表，你建议记录原始凭证，还是只保留每月汇总数字就够了？
  - T034 assistant: 两者可以分层：日常复盘看月度汇总，异常项目保留凭证或备注。这样既不会让表格过重，也能在发现支出突然上升时回到具体来源。

## Q005 / S003

- Question: 只根据本次会话，当前首付日期是什么，之前提到的2027年4月还能作为当前日期吗？
- Topic: updated_down_payment_date
- Target Memory IDs: ['M014(active)', 'M023(superseded)']
- Relevant Turns: ["T037(user, memory_ids=['M014', 'M023'])", 'T038(assistant, memory_ids=[])']
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T055 user: 基金交易记录里的申购费和管理费分别在什么时候体现，账单里应该怎样看？
  - T056 assistant: 申购费通常在买入或份额确认时体现，管理费则从基金资产中按规则计提，不一定在个人账单里单列。看账单时先区分一次性交易费用和持续性运作费用。
  - T057 user: 如果同一只基金有A类和C类份额，只看表面的买入费率会不会误判成本？
  - T058 assistant: 会。A类常见前端申购费，C类通常没有申购费但有销售服务费；持有时间不同，总成本比较结果会变化。应按预计持有期和平台费率表一起计算。

## Q006 / S003

- Question: 只根据本次会话，我当前最多接受多少本金亏损，半年后支付的首付能否主要买新能源行业基金？
- Topic: loss_limit_and_sector_fund_fit
- Target Memory IDs: ['M015(active)', 'M016(active)', 'M025(active)', 'M014(active)', 'M024(superseded)']
- Relevant Turns: ["T041(user, memory_ids=['M015', 'M024'])", "T043(user, memory_ids=['M016'])", 'T045(user, memory_ids=[])', "T046(assistant, memory_ids=['M025'])", 'T047(user, memory_ids=[])', "T048(assistant, memory_ids=['M025'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T055 user: 基金交易记录里的申购费和管理费分别在什么时候体现，账单里应该怎样看？
  - T056 assistant: 申购费通常在买入或份额确认时体现，管理费则从基金资产中按规则计提，不一定在个人账单里单列。看账单时先区分一次性交易费用和持续性运作费用。
  - T057 user: 如果同一只基金有A类和C类份额，只看表面的买入费率会不会误判成本？
  - T058 assistant: 会。A类常见前端申购费，C类通常没有申购费但有销售服务费；持有时间不同，总成本比较结果会变化。应按预计持有期和平台费率表一起计算。

## Q007 / S004

- Question: 只根据本次隐私会话，请告诉我之前说的具体税后月收入，并说明以后还能不能继续使用。
- Topic: deleted_income_refusal
- Target Memory IDs: ['M019(active)', 'M018(deleted)']
- Relevant Turns: ["T061(user, memory_ids=['M019'])", 'T062(assistant, memory_ids=[])', "T059(user, memory_ids=['M018'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T065 user: 如果银行卡里有多张定期存单，到期后自动转存和手动处理有什么区别？
  - T066 assistant: 自动转存会按银行规则把本金或本息续存，省事但利率和期限未必适合当时需求。手动处理能重新比较期限和流动性，但需要自己记得操作。
  - T067 user: 家庭账本里收到报销款时，应该算收入，还是冲减之前那笔支出会更清楚？
  - T068 assistant: 通常按用途处理更清楚：如果报销对应某笔已记录支出，可以冲减该支出；如果没有对应支出，才作为其他收入记录。关键是保持同一规则，便于月底复盘。

## Q008 / S005

- Question: 只根据本次会话，总结我设定的回答结构和表达边界。
- Topic: answer_style_preference
- Target Memory IDs: ['M020(active)', 'M021(active)', 'M022(active)']
- Relevant Turns: ["T069(user, memory_ids=['M020'])", "T071(user, memory_ids=['M021'])", "T073(user, memory_ids=['M022'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T077 user: 基金分红是不是额外赚到的钱，还是只是把基金资产的一部分分给持有人？
  - T078 assistant: 基金分红通常不是额外收益，而是把基金资产的一部分分配给持有人。分红后净值会相应调整，关键仍是总资产变化。
  - T079 user: 买入基金后什么时候确认份额，是按我提交订单那一刻，还是按交易日规则处理？
  - T080 assistant: 通常按交易日和平台规则确认份额，不是按提交订单的瞬间固定。需要看下单时间、是否交易日以及基金公司的确认安排。

## Q009 / S005

- Question: 请按照我本次设定的表达偏好，解释什么是复利，并避免带入无关个人信息。
- Topic: compound_interest_with_style_preference
- Target Memory IDs: ['M020(active)', 'M021(active)', 'M022(active)']
- Relevant Turns: ["T069(user, memory_ids=['M020'])", "T071(user, memory_ids=['M021'])", "T073(user, memory_ids=['M022'])"]
- Direct Answer Leakage: NO
- Semantic Hint Leakage: NO
- Validation: PASS
- Last 4 Messages:
  - T077 user: 基金分红是不是额外赚到的钱，还是只是把基金资产的一部分分给持有人？
  - T078 assistant: 基金分红通常不是额外收益，而是把基金资产的一部分分配给持有人。分红后净值会相应调整，关键仍是总资产变化。
  - T079 user: 买入基金后什么时候确认份额，是按我提交订单那一刻，还是按交易日规则处理？
  - T080 assistant: 通常按交易日和平台规则确认份额，不是按提交订单的瞬间固定。需要看下单时间、是否交易日以及基金公司的确认安排。

