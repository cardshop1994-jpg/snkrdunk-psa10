"""
スニダン PSA10 ポケモンカード相場アプリ
- 売れてる最高値: 過去N日の成約最高価格
- 売れてない最安値: 現在出品中の最安価格

入力は スニダン商品URL / 商品ID が最も確実。
キーワードは「人気ランキング内の部分一致」＋「検索候補サジェスト」で補助する。
（スニダンのフリーワード検索APIはブラウザ内部状態経由のため直接利用不可）
"""
from __future__ import annotations

import os
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


# ---------------- PSA Public API（GEM率） ----------------
PSA_API_BASE = "https://api.psacard.com/publicapi"


def _psa_token() -> Optional[str]:
    """PSA APIトークンを Streamlit Secrets か環境変数から取得（コードには書かない）。"""
    try:
        tok = st.secrets.get("PSA_TOKEN")  # type: ignore[attr-defined]
        if tok:
            return str(tok).strip()
    except Exception:
        pass
    tok = os.environ.get("PSA_TOKEN")
    return tok.strip() if tok else None


def extract_spec_id(text: str) -> Optional[int]:
    """PSA pop の SpecID を、数値 or pop URL から取り出す。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    # pop URL 末尾などの数字（最後に出てくる長めの数字を採用）
    nums = re.findall(r"(\d{4,})", text)
    return int(nums[-1]) if nums else None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_psa_population(spec_id: int) -> Optional[dict]:
    """PSA公式APIから spec_id の鑑定数内訳を取得。トークン未設定/失敗時は None。"""
    token = _psa_token()
    if not token or not spec_id:
        return None
    try:
        r = requests.get(
            f"{PSA_API_BASE}/pop/GetPSASpecPopulation/{spec_id}",
            headers={"Authorization": f"bearer {token}"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or not data.get("PSAPop"):
            return None
        return data
    except Exception:
        return None


def gem_rate(pop: dict) -> Optional[float]:
    """GEM率(%) = PSA10数 ÷ 全鑑定数。"""
    p = (pop or {}).get("PSAPop") or {}
    total = p.get("Total") or 0
    g10 = p.get("Grade10") or 0
    return (g10 / total * 100.0) if total else None


# スニダンのカードID(apparel_id) → PSA SpecID の対応表。
# 一度登録すれば、以降そのカードは SpecID 入力なしで自動的に GEM率 を表示する。
import json as _json

SPEC_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec_map.json")


def load_spec_map() -> dict:
    try:
        with open(SPEC_MAP_FILE, "r", encoding="utf-8") as f:
            return {int(k): int(v) for k, v in _json.load(f).items()}
    except Exception:
        return {}


def save_spec_map(m: dict) -> bool:
    """対応表をファイルに保存（ローカルや永続FSでは保存成功、Streamlit Cloudの一時FSでは
    再起動でリセットされる点に注意。確実な永続化はリポジトリ同梱の spec_map.json で行う）。"""
    try:
        with open(SPEC_MAP_FILE, "w", encoding="utf-8") as f:
            _json.dump({str(k): int(v) for k, v in m.items()}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def resolve_spec_id(apparel_id: int) -> Optional[int]:
    """このカードの SpecID を、セッション登録分→ファイル対応表 の順に解決。"""
    sess_map = st.session_state.get("spec_map_session", {})
    if apparel_id in sess_map:
        return sess_map[apparel_id]
    return load_spec_map().get(apparel_id)


# ---------------- GEM率 自動取得（PriceChartingのPSA pop） ----------------
# スニダンの英語名 → PriceChartingで該当カードを特定 → セットpopページの
# PSA10数/総数から GEM率 を自動算出（手入力不要・PSAトークン不要）。
import html as _htmlmod
import urllib.parse as _urlparse

PC_BASE = "https://www.pricecharting.com"
_RARITY_RE = re.compile(r"\b(RRR|RR|SR|SAR|UR|AR|CHR|CSR|HR|GX)\b")


def _pc_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _parse_pc_query(detail: dict) -> tuple[str, str, Optional[int]]:
    """スニダンの英語名から (検索用フルネーム, セット名, カード番号) を抽出。"""
    nm = detail.get("name") or detail.get("localizedName") or ""
    setm = re.findall(r"\(([^()]*)\)\s*$", nm)
    set_name = setm[-1] if setm else ""
    for junk in ['"', "High Class Pack", "Enhanced Expansion Pack", "Expansion Pack", "Subset"]:
        set_name = set_name.replace(junk, "")
    set_name = re.sub(r"\s+", " ", set_name).strip()
    num = re.search(r"(\d{1,3})\s*/\s*\d{1,3}", nm)
    card_no = int(num.group(1)) if num else None
    full_subj = nm.split("[")[0].strip()
    return full_subj, set_name, card_no


@st.cache_data(ttl=86400, show_spinner=False)
def _pc_set_pop(set_slug: str) -> dict:
    """PriceChartingのセットpopページから {product_slug: (PSA10数, 総数)} を取得（24h キャッシュ）。"""
    sess = _pc_session()
    url = f"{PC_BASE}/pop/set/{_urlparse.quote(set_slug, safe='-&')}"
    try:
        r = sess.get(url, timeout=20)
        r.raise_for_status()
    except Exception:
        return {}
    out: dict = {}
    for m in re.finditer(r"<tr[^>]*>((?:(?!</tr>).)*?)</tr>", r.text, re.S):
        row = m.group(1)
        link = re.search(r'/pop/item/[^/"]+/([^/"?]+)"', row)
        if not link:
            continue
        prod = _htmlmod.unescape(link.group(1))
        nums = [int(x.replace(",", "")) for x in re.findall(r">\s*([\d,]+)\s*<", row)
                if x.strip().replace(",", "").isdigit()]
        if len(nums) >= 2:
            out[prod] = (nums[-2], nums[-1])  # (PSA10, 総数)
    return out


def _pc_resolve(detail: dict) -> Optional[tuple[str, str]]:
    """検索でカードを特定し (set_slug, product_slug) を返す。確証が無ければ None（誤答防止）。"""
    full_subj, set_name, card_no = _parse_pc_query(detail)
    sess = _pc_session()
    try:
        r = sess.get(f"{PC_BASE}/search-products",
                     params={"q": f"{full_subj} {set_name}".strip(), "type": "prices"}, timeout=15)
        h = r.text
    except Exception:
        return None

    def tail_ok(prod: str) -> bool:
        t = re.search(r"-(\d+)$", prod)
        return bool(t) and card_no is not None and int(t.group(1)) == card_no

    base_token = re.sub(_RARITY_RE, "", full_subj).split()
    base_token = base_token[0].lower() if base_token else ""

    rows = re.findall(r'<tr[^>]*data-product="(\d+)"[^>]*>(.*?)</tr>', h, re.S)
    cands = []
    for _pid, row in rows:
        txt = re.sub(r"\s+", " ", _htmlmod.unescape(re.sub(r"<[^>]+>", " ", row))).strip()
        href = re.search(r'/game/([^/"]+)/([^/"?]+)"', row)
        if not href:
            continue
        setslug = _htmlmod.unescape(href.group(1))
        prod = _htmlmod.unescape(href.group(2))
        is_jp = "japanese" in setslug.lower() or "Japanese" in txt
        num_ok = (card_no is not None and f"#{card_no}" in txt) or tail_ok(prod)
        subj_ok = (base_token in txt.lower()) if base_token else True
        if is_jp and num_ok and subj_ok:
            cands.append((setslug, prod))
    cands = list(dict.fromkeys(cands))
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        exact = [c for c in cands if tail_ok(c[1])]
        return exact[0] if len(exact) == 1 else None
    # 単一商品ページへリダイレクトされたケース
    uniq = list(dict.fromkeys(
        (_htmlmod.unescape(s), _htmlmod.unescape(p))
        for s, p in re.findall(r'/game/([^/"]+)/([^/"?]+)"', h)
    ))
    jp_exact = list({(s, p) for s, p in uniq if "japanese" in s.lower() and tail_ok(p)})
    if len(jp_exact) == 1:
        return jp_exact[0]
    jp_any = list({(s, p) for s, p in uniq if "japanese" in s.lower()})
    if len(jp_any) == 1:
        return jp_any[0]
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_gem_auto(apparel_id: int) -> Optional[dict]:
    """カードのGEM率を自動取得。成功時 {rate, psa10, total, set_slug, product_slug}、不可時 None。"""
    try:
        detail = fetch_apparel_detail(apparel_id)
    except Exception:
        return None
    res = _pc_resolve(detail)
    if not res:
        return None
    set_slug, prod = res
    pop = _pc_set_pop(set_slug)
    if prod not in pop:
        return None
    psa10, total = pop[prod]
    if not total:
        return None
    return {
        "rate": round(psa10 / total * 100, 1),
        "psa10": psa10, "total": total,
        "set_slug": set_slug, "product_slug": prod,
    }


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

# スマホ最適化: 狭い画面では横並びカラムを縦積みにし、文字サイズも調整
st.markdown(
    """
    <style>
      /* スマホ(〜640px): すべての横並びカラムを縦積みに */
      @media (max-width: 640px) {
        [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.3rem !important; }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
        [data-testid="stHorizontalBlock"] > [data-testid="column"] {
          flex: 1 1 100% !important; width: 100% !important; min-width: 100% !important;
        }
        .btn-spacer { display: none !important; }          /* PC用のボタン位置合わせをスマホでは無効化 */
        [data-testid="stMetricValue"] { font-size: 1.45rem !important; }
        h1 { font-size: 1.5rem !important; }
        /* 上部はStreamlit固定ヘッダーを避けて十分に空ける（左右だけ詰める） */
        .block-container { padding: 3.2rem 0.8rem 1rem !important; }
      }
      /* テーブルは横スクロール可能に（列が多くても潰れない） */
      [data-testid="stDataFrame"] { overflow-x: auto; }
    </style>
    """,
    unsafe_allow_html=True,
)

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
        st.markdown("<div class='btn-spacer' style='height:1.85em'></div>", unsafe_allow_html=True)  # PCでのラベル高さ合わせ（スマホでは非表示）
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
            f_gem = ex.submit(fetch_gem_auto, selected_id)  # GEM率も並列取得
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
            try:
                gem = f_gem.result()
            except Exception:
                gem = None

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
    m1, m2, m3, m4 = st.columns(4)
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
    with m4:
        if gem:
            st.metric("💎 GEM率", f"{gem['rate']:.1f}%")
            st.caption(f"PSA10 {gem['psa10']:,} / 全{gem['total']:,}枚")
        else:
            st.metric("💎 GEM率", "—")
            st.caption("PSA popを自動特定できず（カード名/型番が特殊な場合）")

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
