"""
三段背离检测模块（S1 + S2 + S3，支持分层递归扩展）
====================================================

这是整个交易体系骨架中"三段结构 / 同向段力度比较 / 背离判定"的
唯一实现。所有上层应用（plot_single.py / plot_3day.py / app.py / main.py
及未来扩展）都应当 import 本模块。

公理化要点
----------
1. 以 MACD 柱状图（hist）的正负号将序列切成连续段。
2. 不足 `min_bars` 根的段视为"噪声"，并入紧邻的反向段；同向段相邻则归并。
   设 min_bars=0 时此步是 no-op。
3. 在归并后的段序列上，按"同向 / 反向 / 同向"三段窗口扫描背离。

分层扩展（hierarchical extension）
----------------------------------
基础结构 P₁ = S1 + S2 + S3。把 P₁ 视作一个复合段：
  - P₁.sign  = S1.sign（也等于 S3.sign）
  - P₁.area  = S1.area + S3.area      （S2 反向，不计）
  - P₁.span  从 S1.start 到 S3.end

在更长序列上可以构造 P₁ + S4 + S5（其中 S4 反向、S5 同向），
再次套用三段背离判定 → 第 2 层背离。
依此类推：P₂ = P₁+S4+S5 又可以扩展为 P₂+S6+S7 → 第 3 层。

第 k 层结构由 2k+1 个原始段组成，其中 k 个同向 + k 个反向（中间 k-1 个 + 最右是反向不存在）
更准确地说：k 个同向段交替 (k-1) 个反向段，再加最末一段同向 → 共 2k+1 段，但
我们把"P + S(2k) + S(2k+1)"按基础三段判定时，P 由前 2k-1 段构成。

触发条件（与基础层完全一致）：
  a. (最右同向段).area / P_k.area  <  ratio_threshold
  b. 底背离: 最右同向段最低价 < P 内所有同向段最低价的最小值
     顶背离: 最右同向段最高价 > P 内所有同向段最高价的最大值

反向屏障规则（opposite-barrier rule）
-------------------------------------
公理起点
~~~~~~~~
"结构什么时候开始"比"结构什么时候结束"更容易定义：

  **触发反向背离 = 下一个同向结构的起点。**

一个同向结构 D 在"起点 → 自己的触发点"这段形成过程中，若跨过另一
个已触发的反向背离 D'，则 D 是无效的——它把"反转之前"和"反转之
后"的两段走势错误地合并到了同一个 P 里。

在这个公理框架下，结构没有"被另一个结构终止"这种事。一段同向运动
要么一直延伸（什么都不是），要么触发自己的背离（成为完成态的 D，
然后接受屏障判定）。"扩展到何时结束"不是这一层要回答的问题。

形式化
~~~~~~
D 被否决，当且仅当存在一个幸存的反向 D' 满足：
  (a) D'.s3_end 严格落在 D 的开区间 (s1_start, s3_end) 之内
      —— "D 形成过程中跨过 D' 的触发点"的可执行形式。
  (b) D' 在同末段位置 (kind, s3_start, s3_end) 上的最高 level > 1
      —— 应用层信号筛选，见下文。

条件 (a) 是公理的直接编码：
  - 锚点 s3_end：反向触发完成的瞬间，新结构起点的自然定义。
  - 开区间：D'.s3_end == D.s1_start 时 D 正好从 D' 之后开始，没有跨过；
            D'.s3_end == D.s3_end 时两者同时触发，几何上不构成跨过。
  - 整个区间：D 的形成跨越 s1_start 到 s3_end 全程，期间任何位置
              出现反向触发都算"跨过"。

条件 (b)：公理之上的应用层筛选
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
公理本身不区分反向 D' 的 level。但实践中：
  - L1 反向背离力度弱，只制造盘整、不真正切换趋势。
  - L1+L1 双重转折（S1+S2+S3 底背离紧接 S2+S3+S4 顶背离）是市场
    转折的典型形态，几何上必然互相"跨过"对方的开区间——若按公理
    无条件适用，两条会互相屏蔽，但它们恰恰都该保留。

所以条件 (b) 限定屏障方必须是趋势级别（L≥2）：L1 反向触发的强度
不足以否决一个跨过它的同向结构。这不是对公理的修正，而是公理之上
的信号筛选层。

"同末段位置的最高 level" 的来历
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
结构扩展中同一末段位置 (kind, s3_start, s3_end) 可能同时触发 L1
和 L≥n。下游 _dedupe_same_terminal 把这些视为同一个背离的不同表
征，保留 level 最大的那条；屏障判定与此一致——按"该位置达到的最高
level"决定屏障资格，而非某条具体记录的 level。这样即便高 level
候选自己被屏蔽掉，同位置幸存的 L1 候选仍按最高 level 发挥屏障作
用；反之，纯 L1（同位置无更高 level 候选）依然不构成屏障。

求解顺序
~~~~~~~~
D' 自己也可能被更内层的反向背离否决，那 D' 就不再是有效屏障。处理
顺序按 s3_end 升序（早触发先定生死）：能屏蔽 D 的 D' 必满足
D'.s3_end < D.s3_end，所以处理早触发候选时所有可能内层屏障都已
定型。一遍线性扫描即可收敛。

L1 同样接受屏障判定（被屏蔽方）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
L1 不作为屏障方，但作为被屏蔽方仍参与过滤。L1 的 S2 那一个反向段
可以充当某个 L≥2 反向背离的 S_last，其末端就是该反向背离的触发点
s3_end，严格落在 L1 的开区间 (S1.start, S3.end) 内——按公理 L1 的
S1 和 S3 分属趋势切换前后两个不同机制，应被屏蔽。

注：条件 (a) 严格强于"反向跨度完全被包含"——若 D' 跨度 ⊆ D 跨度，则
D'.s3_end 必然在 (D.s1_start, D.s3_end] 内。当 D' 起点早于 D（即两者
重叠但互不包含）时，条件 (a) 仍能正确捕获。

对外接口（仍只有一个函数）：
    find_three_segment_divergences(hist, low, high,
                                   min_bars=0,
                                   ratio_threshold=0.5,
                                   max_level=1,
                                   block_by_opposite=True)

参数 max_level：
    1（默认）  只检测基础三段（与历史行为完全一致，向后兼容）。
    2,3,...    同时检测扩展层级。
    None       穷尽所有可能层级（直到段数不够）。

参数 block_by_opposite：
    True（默认）  应用反向屏障规则（用户偏好的语义）。
    False         不过滤，返回全部原始候选（用于调试 / 复现旧行为）。

返回的每条记录新增 'level' 字段，标明触发于第几层（1 = 基础）。
"""
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具：原始分段
# ─────────────────────────────────────────────────────────────────────────────
def find_hist_segments(hist_series):
    """
    将 hist 序列按正负号切成连续段。
    返回 list[dict]，每个 dict：
        { 'sign': 'pos'|'neg', 'start': int, 'end': int,
          'area': float, 'bars': int }
    其中 start / end 为闭区间下标。
    """
    values = hist_series.values
    n = len(values)
    segments = []
    i = 0
    while i < n:
        v = values[i]
        if np.isnan(v):
            i += 1
            continue
        sign = 'neg' if v < 0 else 'pos'
        j = i
        while j < n and not np.isnan(values[j]) and (
            (values[j] < 0  and sign == 'neg') or
            (values[j] >= 0 and sign == 'pos')
        ):
            j += 1
        area = float(np.nansum(np.abs(values[i:j])))
        segments.append({
            'sign':  sign,
            'start': i,
            'end':   j - 1,
            'area':  area,
            'bars':  j - i,
        })
        i = j
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具：噪声合并（min_bars=0 时此函数等价于 no-op）
# ─────────────────────────────────────────────────────────────────────────────
def _merge_short_segments(segs, noise_sign, host_sign, min_bars):
    """
    将不足 min_bars 的 noise_sign 段并入紧邻 host_sign 段。
    优先合并到左侧；左侧不可用合并到右侧；合并后相邻同号段自动归并。
    重复直到稳定。min_bars<=0 时 `bars < min_bars` 永不成立 → 直接返回深拷贝。
    """
    result = [dict(s) for s in segs]
    changed = True
    while changed:
        changed = False
        new_result = []
        skip = set()
        for i, seg in enumerate(result):
            if i in skip:
                continue
            if seg['sign'] == noise_sign and seg['bars'] < min_bars:
                left  = new_result[-1]   if new_result          else None
                right = result[i + 1]    if i + 1 < len(result) else None
                if left is not None and left['sign'] == host_sign:
                    left['end']   = seg['end']
                    left['area'] += seg['area']
                    left['bars'] += seg['bars']
                    changed = True
                elif right is not None and right['sign'] == host_sign:
                    rc = dict(right)
                    rc['start']  = seg['start']
                    rc['area']  += seg['area']
                    rc['bars']  += seg['bars']
                    new_result.append(rc)
                    skip.add(i + 1)
                    changed = True
                else:
                    new_result.append(seg)
            else:
                new_result.append(seg)
        result = new_result
        merged = []
        for seg in result:
            if merged and merged[-1]['sign'] == seg['sign']:
                merged[-1]['end']   = seg['end']
                merged[-1]['area'] += seg['area']
                merged[-1]['bars'] += seg['bars']
                changed = True
            else:
                merged.append(seg)
        result = merged
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具：分层扫描
# ─────────────────────────────────────────────────────────────────────────────
def _scan_levels(segs, p_sign, low_series, high_series,
                 ratio_threshold, max_level, kind, min_bars):
    """
    在 segs（同号交替序列）上扫描第 1..max_level 层背离。

    第 k 层结构占 2k+1 段：
      - 同向段在偏移 0, 2, 4, ..., 2k-2 上（共 k 个）
      - 反向段在偏移 1, 3, ..., 2k-1 上（共 k 个）
      - 最末同向段在偏移 2k 上（即 S_{2k+1} = "S5/S7/..."，下文记为 S_last）

    k=1: 经典 S1 + S2 + S3
    k=2: P₁(S1,S2,S3) + S4 + S5
    k=3: P₂(S1..S5) + S6 + S7
    """
    results = []
    if not segs:
        return results

    upper = max_level if max_level is not None else len(segs)

    for k in range(1, upper + 1):
        window = 2 * k + 1
        if window > len(segs):
            break

        for i in range(len(segs) - window + 1):
            # 检查从 segs[i] 起 window 段，要求严格交替
            block = segs[i:i + window]
            if block[0]['sign'] != p_sign:
                continue
            ok = True
            for j, s in enumerate(block):
                expected = p_sign if (j % 2 == 0) else (
                    'pos' if p_sign == 'neg' else 'neg'
                )
                if s['sign'] != expected:
                    ok = False
                    break
            if not ok:
                # segs 经过合并归并后，相邻同号段不会再出现；
                # 但保险起见仍做一次显式校验
                continue

            same_sign_segs = [block[2 * j] for j in range(k)]   # 0,2,...,2k-2
            S_mid_last     = block[2 * k - 1]                   # 倒数第二段（反向）
            S_last         = block[2 * k]                       # 最末段（同向）

            # min_bars 过滤仅在第 1 层（基础三段）执行；
            # 高层 P 是复合段，bars 必然较大；中间反向段已由合并步骤保证不过短。
            if k == 1 and min(
                same_sign_segs[0]['bars'], S_mid_last['bars'], S_last['bars']
            ) < min_bars:
                continue

            # 面积比测试：S_last.area / P.area
            P_area = sum(s['area'] for s in same_sign_segs)
            if P_area <= 0:
                continue
            ratio = S_last['area'] / P_area
            if ratio >= ratio_threshold:
                continue

            # 价格新低 / 新高测试
            if kind == 'bullish':
                p_low      = min(low_series.iloc[s['start']:s['end'] + 1].min()
                                 for s in same_sign_segs)
                s_last_low = low_series.iloc[S_last['start']:S_last['end'] + 1].min()
                if s_last_low >= p_low:
                    continue
            else:  # bearish
                p_high      = max(high_series.iloc[s['start']:s['end'] + 1].max()
                                  for s in same_sign_segs)
                s_last_high = high_series.iloc[S_last['start']:S_last['end'] + 1].max()
                if s_last_high <= p_high:
                    continue

            # 复合 P 的跨度 / 总根数（包含中间反向段）
            P_start = block[0]['start']
            P_end   = block[2 * k - 2]['end']   # 倒数第三段的尾部就是 P 的尾
            P_bars  = sum(block[j]['bars'] for j in range(0, 2 * k - 1))

            results.append({
                'kind':     kind,
                'level':    k,
                # s1_* : P 的跨度（level=1 时即 S1）
                's1_start': P_start,
                's1_end':   P_end,
                # s3_* : 最末同向段（level=1 时即 S3，level=2 时即 S5）
                's3_start': S_last['start'],
                's3_end':   S_last['end'],
                's1_area':  P_area,
                's3_area':  S_last['area'],
                's1_bars':  P_bars,
                's2_bars':  S_mid_last['bars'],
                's3_bars':  S_last['bars'],
                'ratio':    ratio,
            })

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具：反向屏障过滤
# ─────────────────────────────────────────────────────────────────────────────
def _filter_by_opposite_barriers(divs):
    """
    应用反向屏障规则（顶部 docstring "反向屏障规则" 一节是公理层
    描述，本函数是其可执行形式）。

    公理：触发反向背离 = 下一个同向结构的起点。同向结构 D 在形成
    过程中若跨过另一个已触发的反向 D'，则 D 被否决。

    形式化：D 被否决 ⇔ ∃ 反向 D' 满足
      (a) D'.s3_end ∈ (D.s1_start, D.s3_end)
          （D 形成过程中跨过 D' 的触发点）
      (b) D' 在同末段位置 (kind, s3_start, s3_end) 上的最高 level > 1
          （应用层筛选：L1 反向力度不足以否决跨过它的同向结构）

    实现：把候选按 s3_end 升序处理。能屏蔽 D 的反向 D' 必满足
    D'.s3_end < D.s3_end（触发更早），因此处理早触发的候选时，所有可能
    的内层屏障都已定型。一遍线性扫描即可。

    L1 作为被屏蔽方仍参与过滤
    -------------------------
    早期版本曾让 level<2 无条件幸存，理由是"3 段 r-g-r 或 g-r-g 的开区间
    内只有 1 个反色段，凑不出反向背离"。这个推理偷换了"反向背离"和"反向
    背离的触发点"两个概念。L1 的 S2 那一个反向段，完全可以充当某个 L≥2
    反向背离的 S_last，其末端就是该反向背离的触发点 s3_end，严格落在 L1
    的开区间 (S1.start, S3.end) 内。这正是屏障规则要拦截的场景：L1 的
    两端 S1 和 S3 分属趋势切换前后两个不同机制，不应合并成同一个 P。

    L1 不作为屏障方（条件 b 的依据）
    --------------------------------
    两个相邻的同级 L1 反向背离（如 S1+S2+S3 底背离紧接 S2+S3+S4 顶背离）
    是市场转折的典型双重信号，两条都应保留。这种几何配置与"L1 被 L≥2
    屏蔽"完全对称——区分两者的唯一形式特征是 level。所以条件 (b) 限定
    屏障方必须是 L≥2。这不是对公理的修正，而是公理之上的信号筛选层。

    屏障力度按"同末段位置的最高 level"算
    -------------------------------------
    在结构扩展过程中，同一末段位置 (kind, s3_start, s3_end) 可能同时
    触发 L1 和 L≥n 的背离。下游 _dedupe_same_terminal 把这些候选视为
    同一个背离的不同表征，只标识其中最高 level 的那条。本函数对屏障
    力度的判定与此保持一致：以"该位置在原始候选阶段达到的最高 level"
    决定是否构成屏障，而非按某条具体记录的 level。这样即便高 level
    候选自己被另一个屏障屏蔽掉，同位置幸存的 L1 候选仍按最高 level
    的力度发挥屏障作用；反之，纯 L1（同位置无更高 level 候选）依然
    不构成屏障。
    """
    if not divs:
        return divs

    # 预计算每个 (kind, s3_start, s3_end) 位置在原始候选中达到的最高 level。
    # 屏障力度按此判定，与 _dedupe_same_terminal 的语义保持一致——把同位置
    # 的多 level 候选视为同一个背离的不同表征，按最强表征算屏障力度。
    max_level_at = {}
    for d in divs:
        key = (d['kind'], d['s3_start'], d['s3_end'])
        if d['level'] > max_level_at.get(key, 0):
            max_level_at[key] = d['level']

    # 按触发点（s3_end）升序；同点时按层级升序（仅为稳定排序）
    sorted_divs = sorted(
        divs,
        key=lambda d: (d['s3_end'], d['level']),
    )

    survivors = []
    for d in sorted_divs:
        blocked = False
        for s in survivors:
            if s['kind'] == d['kind']:
                continue   # 同向不构成屏障
            s_key = (s['kind'], s['s3_start'], s['s3_end'])
            if max_level_at.get(s_key, 0) <= 1:
                continue   # 屏障方在同末段位置的最高 level 未达 >1，不构成屏障
            # s 的触发点严格落在 d 跨度开区间内？
            if d['s1_start'] < s['s3_end'] < d['s3_end']:
                blocked = True
                break

        if not blocked:
            survivors.append(d)

    return survivors


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具:末段去重(趋势背离优先于三段背离)
# ─────────────────────────────────────────────────────────────────────────────
def _dedupe_same_terminal(divs):
    """
    同 kind 同末段位置只保留最高 level。

    背景:同一段 S_last 上可能同时触发多个层级的背离——例如 L2 触发时,
    末端 3 段也能构成一个 L1 候选(S1=L2.S3, S3=L2.S5),且因为 L2 的
    价格条件 S5.low < min(S1.low, S3.low) 严格强于该 L1 的 S5.low < S3.low,
    末端 L1 的价格条件自动满足。但面积比 S_last/S3 < 0.5 是独立条件,
    跟 L2 的 S_last/(S1+S3) < 0.5 互不蕴含——前一段可能很大也可能很小,
    所以末端 L1 是否独立成立要单看 S_last/S3。

    语义上趋势背离(L≥2)优先于三段背离(L=1),视觉上也不应在同一根 K 线
    上叠两个百分比。因此同 kind 同 (s3_start, s3_end) 只保留 level 最大的
    那条。

    same_terminal_l1 标记
    ---------------------
    若被合并掉的记录里包含一条 L1(意味着末端 L1 也独立成立 ——
    S_last/前一段 也 <0.5),保留下来的那条会带上 same_terminal_l1=True。
    UI 用此字段画双三角,语义是"力度衰竭在多个尺度上同时成立,信号更强"。
    若 L≥2 触发但末端 L1 不成立(前一段太小,S_last/前一段 >0.5),则
    same_terminal_l1=False,画单三角。
    """
    by_key = {}
    has_l1 = {}        # key -> bool,该 key 下是否出现过 L1 记录
    for d in divs:
        key = (d['kind'], d['s3_start'], d['s3_end'])
        if d['level'] == 1:
            has_l1[key] = True
        if key not in by_key or d['level'] > by_key[key]['level']:
            by_key[key] = d

    out = []
    for key, d in by_key.items():
        d = dict(d)   # 避免修改入参
        # L1 自己不算"同时存在 L1"——这个标记是给被升级保留的 L≥2 用的
        d['same_terminal_l1'] = bool(has_l1.get(key, False)) and d['level'] >= 2
        out.append(d)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 对外主函数
# ─────────────────────────────────────────────────────────────────────────────
def find_three_segment_divergences(hist_series, low_series, high_series,
                                   min_bars=0, ratio_threshold=0.5,
                                   max_level=1, block_by_opposite=True):
    """
    检测 MACD 柱状图上的三段背离结构（含分层扩展和反向屏障过滤）。

    Parameters
    ----------
    hist_series       : pd.Series   MACD 柱状图（正绿负红）
    low_series        : pd.Series   K 线最低价
    high_series       : pd.Series   K 线最高价
    min_bars          : int         每段最少 K 线根数（0 = 不合并、不过滤）
    ratio_threshold   : float       面积比阈值，默认 0.5
    max_level         : int|None    分层扩展深度。
                                    1 = 仅基础三段（默认，向后兼容）
                                    2 = 同时检测 P+S4+S5
                                    None = 穷尽
    block_by_opposite : bool        是否应用反向屏障规则（默认 True）。
                                    背离若跨越一个反向 D'，且 D' 在同末段
                                    位置达到的最高 level≥2，则被否决（纯
                                    L1 反向背离不构成屏障）；置 False 可
                                    拿到未过滤的全部候选。

    Returns
    -------
    list[dict]，按 (s3_start, level) 升序。每条记录字段：
        kind     : 'bullish' | 'bearish'
        level    : 触发层级（1 = 基础三段，2 = P+S4+S5，依此类推）
        s1_start : 左侧主体起始下标（level=1 时即 S1.start）
        s1_end   : 左侧主体结束下标
        s3_start : 右侧最新同向段起始下标
        s3_end   : 右侧最新同向段结束下标
        s1_area  : 左侧主体面积（同向成员面积之和）
        s3_area  : 右侧最新同向段面积
        s1_bars  : 左侧主体跨度（含中间反向段的总根数）
        s2_bars  : 紧邻最末同向段之前的反向段根数
        s3_bars  : 右侧最新同向段根数
        ratio    : s3_area / s1_area
        provisional : bool。True = S_last 的右端点等于数据末尾，意味着该段
                     还可能继续延伸（未来 K 线若同符号会接上、翻号才"封口"），
                     当前的 ratio 和价格新极值都只是快照而非判决。UI 用此
                     字段切配色警示用户。False = 后面已经发生过翻号，
                     S_last 已经定型，信号确定。
        same_terminal_l1 : bool。仅 level≥2 的记录可能为 True。语义为"末端
                     位置上 L1 也独立成立"——即 S_last/前一同向段 < 0.5
                     (注意这个比值与 L≥2 的 S_last/前面所有同向段累计 互不
                     蕴含)。表示力度衰竭在多个尺度上同时成立,信号更强,UI
                     用此字段画双三角。level=1 的记录恒为 False。
    """
    raw_segs = find_hist_segments(hist_series)
    out = []

    # 底背离：P 方向 = neg
    segs_bull = _merge_short_segments(raw_segs, 'neg', 'pos', min_bars)
    out.extend(_scan_levels(segs_bull, 'neg', low_series, high_series,
                            ratio_threshold, max_level,
                            kind='bullish', min_bars=min_bars))

    # 顶背离：P 方向 = pos
    segs_bear = _merge_short_segments(raw_segs, 'pos', 'neg', min_bars)
    out.extend(_scan_levels(segs_bear, 'pos', low_series, high_series,
                            ratio_threshold, max_level,
                            kind='bearish', min_bars=min_bars))

    # 标注"未完成 / 暂定 (provisional)"：
    # 末段 S_last 的右端点等于 hist 序列末尾，意味着该段还可能继续延伸
    # （未来 K 线若同符号会接上、翻号才"封口"），当前的 ratio 只是快照，
    # 不是判决。UI 用此字段切配色（亮黄 + "?" 后缀）警示用户。
    last_index = len(hist_series) - 1
    for d in out:
        d['provisional'] = (d['s3_end'] == last_index)

    # 反向屏障过滤
    if block_by_opposite:
        out = _filter_by_opposite_barriers(out)

    # 末段去重:同 kind 同 (s3_start, s3_end) 只保留最高 level。
    # 趋势背离(L≥2)优先于三段背离(L=1)。屏障过滤之后再去重——确保
    # L≥2 被屏障否决时,同位置的 L1 仍能保留下来。
    # 去重时若发现同位置上 L1 也独立成立,在保留的那条上标记
    # same_terminal_l1=True,UI 据此画双三角(力度多尺度同时衰竭)。
    out = _dedupe_same_terminal(out)

    out.sort(key=lambda d: (d['s3_start'], d['level']))
    return out



# =============================================================================
# 极值补检（独立路径，不与标准三段背离交叉）
# =============================================================================
# 动机
# ----
# MACD 是动量指标——动量拐点先于价格拐点。具体表现：价格的真实顶/底
# 常常落在 hist 已经翻号之后的若干根 K 线上。这种极值落在"反向颜色"
# 的 hist 段里，凑不出标准三段背离要求的"绿-红-绿"或"红-绿-红"结构，
# find_three_segment_divergences 完全检测不到。
#
# 设计原则：完全独立
# ------------------
# 本函数返回的是"另一种"信号，记录字段结构与 find_three_segment_divergences
# 完全不同，不会被混淆进同一个列表。调用方拿到两个独立列表，分别走
# 两条独立的可视化通道。这样做的好处：
# 1. 标准三段背离的算法（屏障 / 去重 / provisional）一行不改
# 2. 即使本函数完全失效，也不会破坏已有的 L1 / L2 信号
# 3. 视觉上用空心三角 △▽（区别于标准背离的实心 ▲▼），读者一眼能区分
#
# 几何判据：相邻反向段比较
# ------------------------
# 顶：绿段 G 紧跟红段 R。若 R 段最高价 > G 段最高价 —— 真实价格高点
#     落在了红段 R 里 —— 则该高点是被标准三段背离漏掉的顶极值。
# 底：红段 R 紧跟绿段 G。若 G 段最低价 < R 段最低价 —— 真实价格低点
#     落在了绿段 G 里 —— 则该低点是被标准三段背离漏掉的底极值。
#
# 为什么是相邻段比较而非全程 running-max/min
# -------------------------------------------
# 全程比较会被"窗口起点本身是个深底"等情况误伤——后续真正的局部大底
# 永远跨不过窗口起点而被漏标。相邻段比较只问"价格有没有越过动量转向
# 前那一段的极值"，与窗口起点无关，且天然自限：盘整中继里的普通反向
# 段凑不出"越过前段极值"的条件，不会刷屏。

# 短反向段过滤阈值。
# hist 在零轴附近的微小抖动会切出 1-3 根的短段，相邻短段之间在趋势中
# 几乎必然满足"后段越过前段"条件（因为趋势本身在创新极值），形成噪声
# 刷屏。前后两段任一长度小于此阈值则跳过该对。
#
# 早期经验值 5 根在持续单边走势中会被频繁过零的短段刷屏。
# 实测 9 根在周/日/小时多数主流周期上噪声显著减少——
# 如需进一步调整，改这一行即可。仅影响 find_missed_extremes；
# 标准三段背离的 min_bars 参数独立控制。
MISSED_EXTREME_MIN_BARS = 9


def find_missed_extremes(hist_series, low_series, high_series,
                         min_bars=MISSED_EXTREME_MIN_BARS):
    """
    补检"价格极值落在反向 hist 段"的背离。

    与 find_three_segment_divergences 是完全独立的两条路径，结果不交叉、
    不去重。调用方拿到的是另一种类型的信号记录。

    Parameters
    ----------
    hist_series : pd.Series   MACD 柱状图
    low_series  : pd.Series   K 线最低价
    high_series : pd.Series   K 线最高价
    min_bars    : int         相邻反向段对的过短过滤阈值（默认 MISSED_EXTREME_MIN_BARS=9）

    Returns
    -------
    list[dict]，每条记录字段（与三段背离的记录字段不同，刻意保持精简）：
        kind      : 'bullish' | 'bearish'
        peak_idx  : int    极值 K 线下标（用于标注定位）
        prev_end  : int    前段（反向颜色）末尾下标
        curr_start: int    当前段（承载极值）起始下标
        curr_end  : int    当前段末尾下标
    """
    segs = find_hist_segments(hist_series)
    results = []

    for i in range(1, len(segs)):
        prev = segs[i - 1]
        curr = segs[i]

        # 过短过滤
        if prev['bars'] < min_bars or curr['bars'] < min_bars:
            continue

        # 顶背离极值：绿段 G(=pos) 紧跟红段 R(=neg)，R 内最高价 > G 内最高价
        if prev['sign'] == 'pos' and curr['sign'] == 'neg':
            prev_high = high_series.iloc[prev['start']:prev['end'] + 1].max()
            curr_window = high_series.iloc[curr['start']:curr['end'] + 1]
            curr_high = curr_window.max()
            if curr_high > prev_high:
                peak_idx = curr['start'] + int(curr_window.values.argmax())
                results.append({
                    'kind':       'bearish',
                    'peak_idx':   peak_idx,
                    'prev_end':   prev['end'],
                    'curr_start': curr['start'],
                    'curr_end':   curr['end'],
                })

        # 底背离极值：红段 R(=neg) 紧跟绿段 G(=pos)，G 内最低价 < R 内最低价
        elif prev['sign'] == 'neg' and curr['sign'] == 'pos':
            prev_low = low_series.iloc[prev['start']:prev['end'] + 1].min()
            curr_window = low_series.iloc[curr['start']:curr['end'] + 1]
            curr_low = curr_window.min()
            if curr_low < prev_low:
                peak_idx = curr['start'] + int(curr_window.values.argmin())
                results.append({
                    'kind':       'bullish',
                    'peak_idx':   peak_idx,
                    'prev_end':   prev['end'],
                    'curr_start': curr['start'],
                    'curr_end':   curr['end'],
                })

    return results

