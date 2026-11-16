# data-qd-146 · 组织温室气体盘查

Traceable scope 1 and 2 greenhouse gas inventory with dated emission factors。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/carbon-inventory --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/carbon_inventory/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## Scope and sample
# Dated activity-based greenhouse gas inventory v1

组织环境数据分析者按活动记录和显式因子表计算Scope 1/2排放。活动字段id/organization/date/scope/category/quantity/unit/factor_id，最多20000；因子最多1000，rate为非负整数有理分数kgCO2e/unit，日期区间[valid_from,valid_to)，null表示无结束。日期严格ISO日历日。活动数量为0..10^12整数，单位必须与因子精确匹配，因子类别和有效期也必须匹配。任何未知/重复因子ID或错配都拒绝；factor来源及版本由调用方负责。
每笔排放=quantity×rate，精确Fraction kgCO2e；按组织和scope分组，同时保留逐活动证据与采用因子。空活动允许，输出空分组。样例用电1000 kWh×2/5=400 kgCO2e，燃气100m³×19/10=190，总计590。
Scope 2活动已由调用方选定单一计量口径；本工具不把location与market-based双列相加，不提供气体级GWP、价值链Scope 3、抵扣、边界核证、货币因素或官方排放因子，也不声称合规盘查。样例虚构，单位换算需提前纳入因子定义。


## Scope and sample
# Dated activity-based greenhouse gas inventory v1

组织环境数据分析者按活动记录和显式因子表计算Scope 1/2排放。活动字段id/organization/date/scope/category/quantity/unit/factor_id，最多20000；因子最多1000，rate为非负整数有理分数kgCO2e/unit，日期区间[valid_from,valid_to)，null表示无结束。日期严格ISO日历日。活动数量为0..10^12整数，单位必须与因子精确匹配，因子类别和有效期也必须匹配。任何未知/重复因子ID或错配都拒绝；factor来源及版本由调用方负责。
每笔排放=quantity×rate，精确Fraction kgCO2e；按组织和scope分组，同时保留逐活动证据与采用因子。空活动允许，输出空分组。样例用电1000 kWh×2/5=400 kgCO2e，燃气100m³×19/10=190，总计590。
Scope 2活动已由调用方选定单一计量口径；本工具不把location与market-based双列相加，不提供气体级GWP、价值链Scope 3、抵扣、边界核证、货币因素或官方排放因子，也不声称合规盘查。样例虚构，单位换算需提前纳入因子定义。


## Scope and sample
# Dated activity-based greenhouse gas inventory v1

组织环境数据分析者按活动记录和显式因子表计算Scope 1/2排放。活动字段id/organization/date/scope/category/quantity/unit/factor_id，最多20000；因子最多1000，rate为非负整数有理分数kgCO2e/unit，日期区间[valid_from,valid_to)，null表示无结束。日期严格ISO日历日。活动数量为0..10^12整数，单位必须与因子精确匹配，因子类别和有效期也必须匹配。任何未知/重复因子ID或错配都拒绝；factor来源及版本由调用方负责。
每笔排放=quantity×rate，精确Fraction kgCO2e；按组织和scope分组，同时保留逐活动证据与采用因子。空活动允许，输出空分组。样例用电1000 kWh×2/5=400 kgCO2e，燃气100m³×19/10=190，总计590。
Scope 2活动已由调用方选定单一计量口径；本工具不把location与market-based双列相加，不提供气体级GWP、价值链Scope 3、抵扣、边界核证、货币因素或官方排放因子，也不声称合规盘查。样例虚构，单位换算需提前纳入因子定义。

