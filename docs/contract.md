# Dated activity-based greenhouse gas inventory v1

组织环境数据分析者按活动记录和显式因子表计算Scope 1/2排放。活动字段id/organization/date/scope/category/quantity/unit/factor_id，最多20000；因子最多1000，rate为非负整数有理分数kgCO2e/unit，日期区间[valid_from,valid_to)，null表示无结束。日期严格ISO日历日。活动数量为0..10^12整数，单位必须与因子精确匹配，因子类别和有效期也必须匹配。任何未知/重复因子ID或错配都拒绝；factor来源及版本由调用方负责。
每笔排放=quantity×rate，精确Fraction kgCO2e；按组织和scope分组，同时保留逐活动证据与采用因子。空活动允许，输出空分组。样例用电1000 kWh×2/5=400 kgCO2e，燃气100m³×19/10=190，总计590。
Scope 2活动已由调用方选定单一计量口径；本工具不把location与market-based双列相加，不提供气体级GWP、价值链Scope 3、抵扣、边界核证、货币因素或官方排放因子，也不声称合规盘查。样例虚构，单位换算需提前纳入因子定义。
