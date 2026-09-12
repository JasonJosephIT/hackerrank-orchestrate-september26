"""Per sample: truth trough vs my end-of-day / intra-day troughs, and the flows on each payday."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))
from datetime import date
from buyorwait.intake import Dataset, build_state
from buyorwait.evidence import image_amounts
from buyorwait.forecast import projection
import buyorwait.forecast as F

ds = Dataset.load(); facts = image_amounts()
for row in ds.samples.itertuples():
    rid, uid, rd = row.request_id, row.user_id, date.fromisoformat(row.request_date)
    st = build_state(ds, uid, rd, facts, request_id=rid)
    req = float(row.requested_amount); safe = float(row.amount_safe_to_pay)
    truth_trough = st.minimum + safe if safe < req else None
    F.INTRADAY_CHECK = False; eod = projection(st)
    F.INTRADAY_CHECK = True; intra = projection(st)
    t_eod = min(b for _, b, _ in eod); d_eod = min(eod, key=lambda t: t[1])[0]
    t_in = min(l for _, _, l in intra); d_in = min(intra, key=lambda t: t[2])[0]
    tt = f"{truth_trough:.2f}" if truth_trough is not None else "full"
    print(f"\n== {rid} {uid} {st.currency} rd={rd} bal={st.balance:.2f} min={st.minimum:.2f} req={req} truth_trough={tt} "
          f"eod={t_eod:.2f}@{d_eod} (diff {t_eod-truth_trough if truth_trough else 0:+.2f}) intra={t_in:.2f}@{d_in} (diff {t_in-truth_trough if truth_trough else 0:+.2f})")
    flows = st.flows(rd, rd.__class__.fromordinal(rd.toordinal()+84))
    bydate = {}
    for d, a, k in flows: bydate.setdefault(d, []).append((a, k))
    recs = {r.key: r for r in st.recurring}
    for d in sorted(bydate):
        fl = bydate[d]
        if any(a > 0 for a, _ in fl) and any(a < 0 for a, _ in fl):
            eod_bal = dict((x, b) for x, b, _ in eod)[d]
            parts = []
            for a, k in fl:
                r = recs.get(k)
                meta = f"cad={r.cadence_days},type={r.event_type},flex={r.flexibility},next={r.next_date}" if r else "fixed"
                parts.append(f"{a:+.2f}[{k}:{meta}]")
            print(f"   payday {d} (rd+{(d-rd).days}) eod_bal={eod_bal:.2f}: " + "  ".join(parts))
