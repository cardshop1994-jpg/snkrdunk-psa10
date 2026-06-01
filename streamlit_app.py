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
def fetch_min_listed_psa10(apparel_id: int) -> Optional[dict]:
    r = _session().get(
        f"{BASE}/v1/apparels/{apparel_id}/used",
        params={
            "perPage": 1,
            "page": 1,
            "isSaleOnly": "true",
            "conditionIds": PSA10_CONDITION_ID,
            "order": "cheaper",
        },
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("apparelUsedItems") or []
    return items[0] if items else None


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
def fetch_sales_history_psa10(
    apparel_id: int, lookback_days: int = LOOKBACK_DAYS, max_pages: int = 10, per_page: int = 200
) -> list[dict]:
    """per_page を大きめにしてページ往復を最小化（API は 200 件/ページまで返す）。
    cutoff より古い成約に達した時点で打ち切る。"""
    sess = _session()
    cutoff = datetime.now() - timedelta(days=lookback_days)
    results: list[dict] = []
    for page in range(1, max_pages + 1):
        r = sess.get(
            f"{BASE}/v1/apparels/{apparel_id}/sales-history",
            params={"page": page, "per_page": per_page, "condition_id": PSA10_CONDITION_ID},
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


def yen(n: Optional[int]) -> str:
    return f"¥{n:,}" if isinstance(n, int) else "—"


# ---------------- UI ----------------

st.set_page_config(page_title="スニダンPSA10相場", page_icon="🎴", layout="wide")
st.title("🎴 スニダン PSA10 ポケカ相場")
st.caption("売れてる最高値（過去N日の成約）と 売れてない最安値（現在の最安出品）を算出")

with st.sidebar:
    st.subheader("対象カードの指定")
    raw = st.text_input(
        "キーワード / スニダンURL / 商品ID",
        value=st.session_state.get("raw", ""),
        placeholder="例: ミュウ 054  /  リザードン SAR  /  https://snkrdunk.com/apparels/704401",
    )
    lookback = st.number_input("成約集計の期間（日）", 7, 365, LOOKBACK_DAYS, step=7)
    go = st.button("相場を出す", type="primary", use_container_width=True)
    st.markdown(
        "---\n**キーワード検索対応。** カード名＋型番（例: `ミュウ 054`）で検索できます。\n\n"
        "URL（`https://snkrdunk.com/apparels/...`）や商品IDを直接貼ってもOK。"
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

    with st.spinner("相場取得中…"):
        # 最安出品 と 成約履歴 は独立なので並列取得して待ち時間を短縮
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_min = ex.submit(fetch_min_listed_psa10, selected_id)
            f_hist = ex.submit(fetch_sales_history_psa10, selected_id, int(lookback))
            try:
                min_listed = f_min.result()
            except Exception:
                min_listed = None
            try:
                history = f_hist.result()
            except Exception:
                history = []

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

    prices = [h["price"] for h in history if isinstance(h.get("price"), int)]
    sold_max = max(prices) if prices else None
    sold_max_entry = max(history, key=lambda h: h.get("price", 0)) if prices else None
    sold_med = sorted(prices)[len(prices) // 2] if prices else None
    listed_price = (min_listed or {}).get("price")

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric(f"🔥 売れてる最高値（{int(lookback)}日）", yen(sold_max))
        if sold_max_entry:
            st.caption(f"成約: {sold_max_entry.get('date')} ・ 件数{len(prices)} ・ 中央値{yen(sold_med)}")
        elif not history:
            st.caption("対象期間に成約なし")
    with m2:
        st.metric("🧊 売れてない最安値（現在出品）", yen(listed_price))
        if min_listed:
            st.caption(f"出品ID: {min_listed.get('id')}")
        else:
            st.caption("PSA10の出品なし")
    with m3:
        gap = (listed_price - sold_max) if (sold_max and isinstance(listed_price, int)) else None
        st.metric("📐 最安出品 − 最高成約", yen(gap), delta=f"{gap:+,}円" if gap is not None else None)
        st.caption("マイナス＝最高成約より安く出ている")

    with st.expander(f"成約履歴 {len(history)} 件（過去{int(lookback)}日 / PSA10）"):
        if history:
            st.dataframe(
                [
                    {"成約": h.get("date"), "価格": h.get("price"), "状態": h.get("label")}
                    for h in history
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("成約なし")
elif not go:
    st.info("左のサイドバーに スニダンURL・商品ID・キーワード のいずれかを入れて『相場を出す』を押してください。")
