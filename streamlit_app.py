"""
スニダン PSA10 ポケモンカード相場アプリ
- 売れてる最高値: 過去N日の成約最高価格
- 売れてない最安値: 現在出品中の最安価格

入力は スニダン商品URL / 商品ID が最も確実。
キーワードは「人気ランキング内の部分一致」＋「検索候補サジェスト」で補助する。
（スニダンのフリーワード検索APIはブラウザ内部状態経由のため直接利用不可）
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
import streamlit as st

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BASE = "https://snkrdunk.com"
PSA10_CONDITION_ID = 22
SEARCH_CATEGORY_TCG = 6
LOOKBACK_DAYS = 90

# スニダンの状態(condition)ID。素体(未鑑定)はランクA〜D、鑑定はPSA等。
RAW_CONDITION_IDS = [18, 19, 20, 21]  # A / B / C / D（素体・未鑑定）
PSA9_CONDITION_ID = 23
# 表示するグレード定義: (ラベル, 成約履歴用のcondition_idリスト, 最安出品用のconditionIds文字列)
GRADES = [
    ("素体", RAW_CONDITION_IDS, ",".join(map(str, RAW_CONDITION_IDS))),
    ("PSA9", [PSA9_CONDITION_ID], str(PSA9_CONDITION_ID)),
    ("PSA10", [PSA10_CONDITION_ID], str(PSA10_CONDITION_ID)),
]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.9"})
    return s


@dataclass
class Candidate:
    apparel_id: int
    name: str


def extract_apparel_id(text: str) -> Optional[int]:
    """URL または数値から apparelId を取り出す。"""
    text = text.strip()
    m = re.search(r"/apparels/(\d+)", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None


# title → link → …(同じ商品オブジェクト内)… → brandId を一括で取る
_ITEM_RE = re.compile(
    r'"title":"((?:[^"\\]|\\.)*)","link":"https://snkrdunk\.com/apparels/(\d+)"'
    r'(?P<rest>.*?)"brandId":"(?P<brand>[^"]+)"',
    re.S,
)


@st.cache_data(ttl=600, show_spinner=False)
def search_keyword(keyword: str, brand: str = "pokemon") -> list[Candidate]:
    """スニダンのフリーワード検索。検索ページ(/search?keywords=...)はRSCで
    結果をサーバーレンダリングするため、RSCペイロードを取得して
    商品名(title)と apparelId(link) のペアを抽出する。
    ※検索パラメータは `keywords`（複数形）。`q`/`keyword` ではヒットしない。
    brand を指定するとそのブランド（既定: pokemon）のみに絞り込む。"""
    keyword = keyword.strip()
    if not keyword:
        return []
    sess = _session()
    try:
        r = sess.get(
            f"{BASE}/search",
            params={"keywords": keyword},
            headers={"RSC": "1"},
            timeout=20,
        )
        r.raise_for_status()
        txt = r.content.decode("utf-8", errors="replace")
    except Exception:
        return []
    out: list[Candidate] = []
    seen: set[int] = set()
    for m in _ITEM_RE.finditer(txt):
        # 同じオブジェクト内に別商品が割り込んでいたら除外（restにtitleが無いこと）
        if '"title":"' in m.group("rest"):
            continue
        if brand and m.group("brand") != brand:
            continue
        aid = int(m.group(2))
        if aid in seen:
            continue
        seen.add(aid)
        raw_title = m.group(1)
        title = raw_title.encode().decode("unicode_escape", "ignore") if "\\u" in raw_title else raw_title
        out.append(Candidate(apparel_id=aid, name=title))
    return out


@st.cache_data(ttl=600, show_spinner=False)
def fetch_keyword_suggestions(keyword: str) -> list[str]:
    if not keyword.strip():
        return []
    sess = _session()
    try:
        r = sess.get(
            f"{BASE}/v3/search/suggestions",
            params={"keyword": keyword.strip(), "limit": 10},
            timeout=10,
        )
        r.raise_for_status()
        return [s.get("keyword", "") for s in r.json().get("suggestions", []) if s.get("keyword")]
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def fetch_apparel_detail(apparel_id: int) -> dict:
    r = _session().get(f"{BASE}/v1/apparels/{apparel_id}", timeout=15)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_min_listed(apparel_id: int, condition_ids: str) -> Optional[dict]:
    """指定状態の現在出品中で最安の1件を返す。condition_ids はカンマ区切り可（例: '18,19,20,21'）。"""
    r = _session().get(
        f"{BASE}/v1/apparels/{apparel_id}/used",
        params={
            "perPage": 1,
            "page": 1,
            "isSaleOnly": "true",
            "conditionIds": condition_ids,
            "order": "cheaper",
        },
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("apparelUsedItems") or []
    return items[0] if items else None


def fetch_min_listed_psa10(apparel_id: int) -> Optional[dict]:
    return fetch_min_listed(apparel_id, str(PSA10_CONDITION_ID))


_REL_RE = re.compile(r"^(\d+)\s*(時間|日|週間|ヶ月|か月|年)前$")
_ABS_RE = re.compile(r"^(\d{4})[/年](\d{1,2})[/月](\d{1,2})日?$")


def parse_relative_date(s: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """相対表現（例: 1日前 / 4時間前 / たった今）と
    絶対日付（例: 2026/05/11 / 2026年5月11日）の両方を解釈する。
    スニダンは直近を相対、古い成約を絶対日付で返すため両対応が必須。"""
    now = now or datetime.now()
    s = (s or "").strip()
    if s in ("たった今", "今"):
        return now
    m = _REL_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return {
            "時間": now - timedelta(hours=n),
            "日": now - timedelta(days=n),
            "週間": now - timedelta(weeks=n),
            "ヶ月": now - timedelta(days=30 * n),
            "か月": now - timedelta(days=30 * n),
            "年": now - timedelta(days=365 * n),
        }[unit]
    a = _ABS_RE.match(s)
    if a:
        try:
            return datetime(int(a.group(1)), int(a.group(2)), int(a.group(3)))
        except ValueError:
            return None
    return None


@st.cache_data(ttl=120, show_spinner=False)
def fetch_sales_history(
    apparel_id: int, condition_id: int, lookback_days: int = LOOKBACK_DAYS,
    max_pages: int = 10, per_page: int = 200,
) -> list[dict]:
    """指定状態(condition_id 単一)の成約履歴を lookback_days 分だけ返す。
    per_page を大きめにしてページ往復を最小化（API は 200 件/ページまで）。
    cutoff より古い成約に達した時点で打ち切る。
    ※ sales-history はカンマ区切り複数IDを受け付けないため condition_id は1つだけ。"""
    sess = _session()
    cutoff = datetime.now() - timedelta(days=lookback_days)
    results: list[dict] = []
    for page in range(1, max_pages + 1):
        r = sess.get(
            f"{BASE}/v1/apparels/{apparel_id}/sales-history",
            params={"page": page, "per_page": per_page, "condition_id": condition_id},
            timeout=15,
        )
        r.raise_for_status()
        history = r.json().get("history") or []
        if not history:
            break
        oldest: Optional[datetime] = None
        for h in history:
            dt = parse_relative_date(h.get("date", ""))
            h["_dt"] = dt.isoformat() if dt else None
            if dt and dt >= cutoff:
                results.append(h)
            if dt and (oldest is None or dt < oldest):
                oldest = dt
        if oldest and oldest < cutoff:
            break
        if len(history) < per_page:
            break
    return results


def fetch_sales_history_multi(
    apparel_id: int, condition_ids: list[int], lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """複数状態（素体A〜D等）の成約履歴をまとめて取得（各IDを並列取得して結合）。"""
    if len(condition_ids) == 1:
        return fetch_sales_history(apparel_id, condition_ids[0], lookback_days)
    merged: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(condition_ids)) as ex:
        futs = [ex.submit(fetch_sales_history, apparel_id, cid, lookback_days) for cid in condition_ids]
        for f in futs:
            try:
                merged.extend(f.result())
            except Exception:
                pass
    return merged


def fetch_sales_history_psa10(apparel_id: int, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    return fetch_sales_history(apparel_id, PSA10_CONDITION_ID, lookback_days)


def yen(n: Optional[int]) -> str:
    return f"¥{n:,}" if isinstance(n, int) else "—"


# ---------------- UI ----------------

st.set_page_config(page_title="スニダンPSA10相場", page_icon="🎴", layout="wide")
st.title("🎴 スニダン ポケカ 相場アプリ(素体・PSA10)")
st.caption("売れてる最高値（過去N日の成約）と 売れてない最安値（現在の最安出品）を算出")

with st.form("search_form", clear_on_submit=False):
    col_kw, col_days, col_btn = st.columns([6, 2, 2])
    with col_kw:
        raw = st.text_input(
            "キーワード / スニダンURL / 商品ID",
            value=st.session_state.get("raw", ""),
            placeholder="例: ミュウ 054  /  リザードン SAR  /  https://snkrdunk.com/apparels/704401",
        )
    with col_days:
        lookback = st.number_input("成約集計の期間（日）", 7, 365, LOOKBACK_DAYS, step=7)
    with col_btn:
        st.markdown("<div style='height:1.85em'></div>", unsafe_allow_html=True)  # ラベル高さ合わせ
        go = st.form_submit_button("🔍 相場を出す", type="primary", use_container_width=True)
st.caption(
    "カード名＋型番（例: `ミュウ 054`）で検索 / "
    "スニダンのURL（`https://snkrdunk.com/apparels/...`）や商品IDを直接貼ってもOK。Enterでも検索できます。"
)

selected_id: Optional[int] = None

if go and raw.strip():
    st.session_state["raw"] = raw
    selected_id = extract_apparel_id(raw)
    if selected_id is None:
        # キーワード扱い（フリーワード検索）
        kw = raw.strip()
        matches = search_keyword(kw)
        st.session_state["matches"] = [(c.apparel_id, c.name) for c in matches]
        st.session_state["suggestions"] = fetch_keyword_suggestions(kw)
        st.session_state.pop("direct_id", None)
    else:
        st.session_state["direct_id"] = selected_id
        st.session_state.pop("matches", None)

# 直接ID指定
if st.session_state.get("direct_id"):
    selected_id = st.session_state["direct_id"]

# キーワード候補
matches = st.session_state.get("matches")
if matches is not None and selected_id is None:
    sugg = st.session_state.get("suggestions") or []
    if sugg:
        st.caption("検索候補ワード: " + " / ".join(sugg))
    if matches:
        st.subheader(f"検索結果 {len(matches)} 件")
        lbl = {f"[{i}] {n}": i for i, n in matches}
        choice = st.radio("対象カードを選択", list(lbl.keys()), label_visibility="collapsed")
        selected_id = lbl[choice]
    else:
        st.warning(
            "一致するカードが見つかりませんでした。キーワードを変えるか、"
            "スニダンの商品URL（例: `https://snkrdunk.com/apparels/704401`）を貼ってください。"
        )

# 相場表示
if selected_id:
    try:
        detail = fetch_apparel_detail(selected_id)
    except Exception as e:
        st.error(f"商品情報の取得に失敗しました（ID={selected_id}）: {e}")
        st.stop()

    with st.spinner("相場取得中…（素体 / PSA9 / PSA10）"):
        lb = int(lookback)
        # 全グレードの最安出品・成約履歴を一括並列取得
        with ThreadPoolExecutor(max_workers=8) as ex:
            f_listed = {
                label: ex.submit(fetch_min_listed, selected_id, cond_str)
                for label, _cids, cond_str in GRADES
            }
            f_hist = {
                label: ex.submit(fetch_sales_history_multi, selected_id, cids, lb)
                for label, cids, _cond_str in GRADES
            }
            grade_data = {}
            for label, _cids, _cond_str in GRADES:
                try:
                    ml = f_listed[label].result()
                except Exception:
                    ml = None
                try:
                    hist = f_hist[label].result()
                except Exception:
                    hist = []
                grade_data[label] = {"min_listed": ml, "history": hist}

    st.divider()
    c0, c1 = st.columns([1, 3])
    with c0:
        img = (detail.get("primaryMedia") or {}).get("imageUrl")
        if img:
            st.image(img, width=170)
    with c1:
        st.markdown(f"### {detail.get('localizedName') or detail.get('name')}")
        st.caption(
            f"ID: `{detail.get('id')}` ・ 型番: `{detail.get('productNumber') or '-'}` "
            f"・ 発売: {detail.get('displayReleasedAt') or '-'}"
        )
        st.link_button("スニダンで開く", f"{BASE}/apparels/{selected_id}")

    # 各グレードの指標を計算
    def metrics_of(d: dict) -> dict:
        prices = [h["price"] for h in d["history"] if isinstance(h.get("price"), int)]
        sold_max = max(prices) if prices else None
        sold_med = sorted(prices)[len(prices) // 2] if prices else None
        listed = (d["min_listed"] or {}).get("price")
        gap = (listed - sold_max) if (sold_max and isinstance(listed, int)) else None
        return {
            "sold_max": sold_max, "sold_med": sold_med, "count": len(prices),
            "listed": listed, "gap": gap,
        }

    computed = {label: metrics_of(grade_data[label]) for label, *_ in GRADES}

    st.subheader(f"グレード別相場（成約は過去{lb}日）")
    table_rows = []
    for label, *_ in GRADES:
        m = computed[label]
        table_rows.append({
            "状態": label,
            "🔥売れてる最高値": yen(m["sold_max"]),
            "🧊売れてない最安値": yen(m["listed"]),
            "成約件数": m["count"],
            "成約中央値": yen(m["sold_med"]),
            "最安−最高(差額)": (f"{m['gap']:+,}円" if m["gap"] is not None else "—"),
        })
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
    st.caption(
        "「素体」はランクA〜Dの未鑑定カードを合算（最安値は状態を問わず最も安い出品）。"
        "🔥=実際に売れた最高値 / 🧊=現在出品中の最安値。差額がマイナス＝最高成約より安く出ている。"
    )

    # PSA10は従来どおりメトリクスでも強調表示
    pm = computed["PSA10"]
    st.markdown("#### PSA10 詳細")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric(f"🔥 売れてる最高値（{lb}日）", yen(pm["sold_max"]))
        st.caption(f"件数{pm['count']} ・ 中央値{yen(pm['sold_med'])}" if pm["count"] else "対象期間に成約なし")
    with m2:
        st.metric("🧊 売れてない最安値（現在出品）", yen(pm["listed"]))
        st.caption("PSA10の出品あり" if pm["listed"] else "PSA10の出品なし")
    with m3:
        st.metric("📐 最安出品 − 最高成約", yen(pm["gap"]),
                  delta=f"{pm['gap']:+,}円" if pm["gap"] is not None else None)
        st.caption("マイナス＝最高成約より安く出ている")

    for label, *_ in GRADES:
        hist = grade_data[label]["history"]
        with st.expander(f"{label} の成約履歴 {len(hist)} 件（過去{lb}日）"):
            if hist:
                st.dataframe(
                    [
                        {"成約": h.get("date"), "価格": h.get("price"),
                         "状態": h.get("condition"), "区分": h.get("label")}
                        for h in sorted(hist, key=lambda x: x.get("_dt") or "", reverse=True)
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.write("成約なし")
elif not go:
    st.info("上の検索窓に スニダンURL・商品ID・キーワード のいずれかを入れて『相場を出す』を押してください。")
