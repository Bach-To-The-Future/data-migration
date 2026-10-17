"""PROTOTYPE for issue 9: render report-data.json into the report variants. Throwaway.

    python prototype/report/render.py

Writes prototype/report/out/: one static HTML file per variant (no scripts, so the same file
renders to PDF), plus index.html, a viewer with a floating switcher (?doc=signoff&variant=A).
Every page is a pure function of report-data.json: no clock, no randomness, stable ordering.
"""
import html
import json
import pathlib

HERE = pathlib.Path(__file__).parent
OUT = HERE / "out"

# Reader-facing status, derived from the testing module's verdict and the validator's result.
# group: what a non-technical reader is asked to do with it.
def status(a):
    v, val = a["verdict"], a["validation"]
    if v == "Reconciled" and val == "Passed":
        notes = [f for f in a["flags"] if f["kind"] in ("pre_existing", "single_writer")]
        return ("Converted", "ready", "with notes" if notes else "")
    if v == "Reconciled" and val == "Failed":
        return ("Failed final validation", "our_work", "")
    if v == "Failed":
        return (f"Failed: {a['failure']['dimension']}", "our_work", "")
    if v.startswith("Blocked by a dependency"):
        return (f"Blocked by {a['blocked_by']}", "waiting", "")
    if v.startswith("Not testable"):
        return ("Contains manual work", "manual", "")
    if v == "Recognised":
        return ("Recognised, not converted", "manual", "")
    return (v, "our_work", "")

GROUPS = {
    "ready": ("Converted and checked", "Ready to use. Converted code behaved exactly like the original on every test."),
    "our_work": ("Not converted yet: our work", "We are fixing these. Nothing is needed from you."),
    "waiting": ("Waiting on another item", "These depend on an item above and are checked once it is fixed."),
    "manual": ("Manual work", "The tool cannot convert these. A person has to."),
}
ORDER = ["ready", "our_work", "waiting", "manual"]

def esc(s):
    return html.escape(str(s))

def page(title, body, extra_css=""):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="dcterms.created" content="{esc(DATA['run']['finished'])}"><meta name="dcterms.modified" content="{esc(DATA['run']['finished'])}"><title>{esc(title)}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #666; }} }}
body {{ font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10pt; color: #1a1a1a; line-height: 1.45; max-width: 820px; margin: 0 auto; padding: 16px; }}
h1 {{ font-size: 17pt; margin: 0 0 4px; }} h2 {{ font-size: 13pt; margin: 22px 0 6px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }}
h3 {{ font-size: 11pt; margin: 16px 0 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 6px 0; }} th, td {{ border: 1px solid #ccc; padding: 4px 6px; text-align: left; vertical-align: top; font-size: 9pt; }}
th {{ background: #f2f2f2; }} .muted {{ color: #555; }} .tag {{ display: inline-block; border: 1px solid #888; border-radius: 3px; padding: 0 4px; font-size: 8pt; margin-right: 3px; }}
.ready {{ color: #1b6e20; }} .our_work {{ color: #9a3412; }} .waiting {{ color: #6b4f00; }} .manual {{ color: #444; }}
.banner {{ border: 2px solid #9a3412; padding: 6px 10px; font-weight: bold; margin-bottom: 10px; }}
.sign {{ border: 1px solid #999; padding: 10px; margin-top: 18px; }} .sign td {{ height: 26px; }}
.bar {{ display: flex; height: 14px; margin: 6px 0 2px; }} .bar span {{ display: block; height: 14px; }}
{extra_css}
</style></head><body>
<p class="muted" style="font-size:8pt">PROTOTYPE, invented data. Report data {esc(DATA['run']['id'])}.</p>
{body}
</body></html>
"""

def header_lines(r):
    lines = []
    if r["preview"]:
        lines.append('<div class="banner">PREVIEW RUN: reconciliation was off, so nothing in this report counts as converted.</div>')
    return "".join(lines)

def counts():
    c = {g: [] for g in ORDER}
    for a in DATA["artefacts"]:
        c[status(a)[1]].append(a)
    return c

def stacked_bar(c, total):
    colours = {"ready": "#2e7d32", "our_work": "#c2410c", "waiting": "#b8860b", "manual": "#888"}
    spans = "".join(f'<span style="width:{100*len(c[g])/total:.1f}%;background:{colours[g]}"></span>' for g in ORDER if c[g])
    return f'<div class="bar">{spans}</div>'

def sign_block(scope="this run"):
    return f"""<div class="sign"><b>Sign-off for {esc(scope)}</b>
<table><tr><th style="width:30%">Name</th><th style="width:30%">Role</th><th style="width:20%">Date</th><th>Signature</th></tr>
<tr><td></td><td>Client</td><td></td><td></td></tr><tr><td></td><td>Delivery lead</td><td></td><td></td></tr></table></div>"""

# ---------- Client sign-off, variant A: a one-page verdict letter ----------
def signoff_a():
    r, c = DATA["run"], counts()
    total = len(DATA["artefacts"])
    body = header_lines(r)
    body += f"<h1>Conversion sign-off: {esc(r['engagement'])}</h1>"
    body += f"<p class='muted'>{esc(r['source']['platform'])} to {esc(r['target']['platform'])}. Run finished {esc(r['finished'][:10])}.</p>"
    body += f"<p style='font-size:13pt'><b>{len(c['ready'])} of {total} items are converted and checked.</b> {len(c['our_work']) + len(c['waiting'])} are still with us, and {len(c['manual'])} need manual work.</p>"
    body += stacked_bar(c, total)
    for g in ORDER:
        if not c[g]:
            continue
        title, line = GROUPS[g]
        body += f"<h2 class='{g}'>{esc(title)} ({len(c[g])})</h2><p class='muted'>{esc(line)}</p><ul>"
        for a in c[g]:
            s = status(a)
            note = ""
            if g == "ready" and s[2]:
                note = " <span class='tag'>see note</span>"
            if g == "our_work" and a.get("failure"):
                note = f" {esc(a['failure']['plain'])}"
            body += f"<li><b>{esc(a['plain'])}</b> <span class='muted'>({esc(a['name'])})</span>{note}</li>"
        body += "</ul>"
    body += "<h2>What we need from you</h2><ul>" + "".join(f"<li><b>{esc(n['what'])}.</b> {esc(n['why'])}</li>" for n in DATA["needs_client"]) + "</ul>"
    notes = [(a, f) for a in DATA["artefacts"] for f in a["flags"] if f["kind"] == "pre_existing"]
    if notes:
        body += "<h2>Issues we found in the existing system</h2><p class='muted'>These are in your current code, not caused by the conversion. The converted code behaves the same way.</p><ul>"
        body += "".join(f"<li><b>{esc(a['plain'])}</b> {esc(f['text'])}</li>" for a, f in notes) + "</ul>"
    body += f"<h2>One condition for the converted code</h2><p>{esc(r['single_writer'])}</p>"
    body += sign_block()
    return page("Sign-off (A: verdict letter)", body)

# ---------- Client sign-off, variant B: by business area, sign-off per area ----------
def signoff_b():
    r = DATA["run"]
    body = header_lines(r)
    body += f"<h1>{esc(r['engagement'])}: conversion by business area</h1><p class='muted'>Each area is signed off on its own.</p>"
    areas = sorted({a["area"] for a in DATA["artefacts"]})
    body += "<table><tr><th>Area</th><th>Converted</th><th>With us</th><th>Manual</th></tr>"
    for ar in areas:
        items = [a for a in DATA["artefacts"] if a["area"] == ar]
        n = lambda gs: sum(1 for a in items if status(a)[1] in gs)
        body += f"<tr><td>{esc(ar)}</td><td>{n(['ready'])}</td><td>{n(['our_work','waiting'])}</td><td>{n(['manual'])}</td></tr>"
    body += "</table>"
    for ar in areas:
        items = [a for a in DATA["artefacts"] if a["area"] == ar]
        body += f"<h2>{esc(ar)}</h2><table><tr><th style='width:45%'>What it does</th><th>Status</th><th>In plain words</th></tr>"
        for a in items:
            s = status(a)
            why = a.get("failure", {}).get("plain") or next((f["text"] for f in a["flags"] if f["kind"] in ("manual", "pre_existing")), "") or GROUPS[s[1]][1]
            body += f"<tr><td>{esc(a['plain'])}<br><span class='muted'>{esc(a['name'])}</span></td><td class='{s[1]}'><b>{esc(s[0])}</b></td><td>{esc(why)}</td></tr>"
        body += "</table>"
        body += sign_block(f"the {ar} area")
    body += "<h2>What we need from you</h2><ul>" + "".join(f"<li><b>{esc(n['what'])}.</b> {esc(n['why'])}</li>" for n in DATA["needs_client"]) + "</ul>"
    body += f"<p class='muted'>Condition for all converted code: {esc(r['single_writer'])}</p>"
    return page("Sign-off (B: by business area)", body)

# ---------- Client sign-off, variant C: questions and answers ----------
def signoff_c():
    r, c = DATA["run"], counts()
    total = len(DATA["artefacts"])
    qa = []
    qa.append(("Is our code converted?", f"{len(c['ready'])} of {total} items are converted, and each was checked by running it against your original on test data, then checked again by an independent second check."))
    qa.append(("What is not converted yet, and why?", "<ul>" + "".join(f"<li><b>{esc(a['plain'])}</b>: {esc(status(a)[0])}. {esc(a.get('failure', {}).get('plain', ''))}</li>" for a in c["our_work"] + c["waiting"]) + "</ul>We are working on these. Nothing is needed from you."))
    qa.append(("What will a person have to do by hand?", "<ul>" + "".join(f"<li><b>{esc(a['plain'])}</b>: {esc(next((f['text'] for f in a['flags'] if f['kind']=='manual'), ''))}</li>" for a in c["manual"]) + "</ul>"))
    qa.append(("What do you need from us?", "<ul>" + "".join(f"<li><b>{esc(n['what'])}.</b> {esc(n['why'])}</li>" for n in DATA["needs_client"]) + "</ul>"))
    qa.append(("Did you find problems in our current system?", "<ul>" + "".join(f"<li>{esc(f['text'])}</li>" for a in DATA['artefacts'] for f in a['flags'] if f['kind']=='pre_existing') + "</ul>The converted code behaves the same way, so nothing changes until you decide to fix it."))
    qa.append(("Is there anything the converted code relies on?", esc(r["single_writer"])))
    qa.append(("What happened to our code during conversion?", f"It was processed under a {esc(r['retention']['policy'])} arrangement at {esc(r['retention']['location'])}. {esc(r['retention']['remaining_exposure'])}"))
    body = header_lines(r) + f"<h1>{esc(r['engagement'])}: your questions answered</h1>"
    body += "".join(f"<h2>{esc(q)}</h2><div>{a}</div>" for q, a in qa)
    body += sign_block()
    return page("Sign-off (C: questions and answers)", body)

# ---------- Engineering report, variant A: artefact by artefact ----------
def eng_run_header():
    r = DATA["run"]
    s, t, v, m, ret = r["source"], r["target"], r["versions"], r["model_use"], r["retention"]
    rows = [
        ("Run", f"{r['id']}, {r['started']} to {r['finished']}" + (" (PREVIEW)" if r["preview"] else "")),
        ("Source", f"{s['platform']}: client build {s['client_build']}, sandbox build {s['sandbox_build']}, compatibility level {s['compatibility_level']}"),
        ("Target", f"{t['platform']}, runtime {t['runtime']}, library {t['library']}"),
        ("Versions", ", ".join(f"{k} {val}" for k, val in sorted(v.items()))),
        ("Retention", f"{ret['policy']} at {ret['location']}: {ret['checked']}. {ret['remaining_exposure']}"),
        ("Model use", f"{m['spent']:.2f} of {m['ceiling']:.2f} {m['currency']}; steps by tier {m['steps_by_tier']}; skipped for budget {m['skipped_for_budget']}; refusals {len(m['refusals'])}"),
        ("Single-writer assumption", r["single_writer"]),
    ]
    return "<table>" + "".join(f"<tr><th style='width:22%'>{esc(k)}</th><td>{esc(val)}</td></tr>" for k, val in rows) + "</table>"

def eng_artefact(a):
    s = status(a)
    out = f"<h3>{esc(a['name'])} <span class='muted'>({esc(a['type'])})</span></h3>"
    out += "<table><tr><th>Status</th><th>Verdict (testing module)</th><th>Validation (final validator)</th></tr>"
    out += f"<tr><td class='{s[1]}'><b>{esc(s[0])}</b></td><td>{esc(a['verdict'])}</td><td>{esc(a['validation'])}</td></tr></table>"
    for f in a["flags"]:
        label = {"single_writer": "relies on the single-writer assumption", "pre_existing": "pre-existing issue", "raised": "raised form", "fallback": "fallback", "manual": "manual"}[f["kind"]]
        out += f"<p><span class='tag'>{esc(label)}</span>{esc(f.get('text', ''))}</p>"
    if a.get("failure"):
        fl = a["failure"]
        out += f"<p><b>Failure ({esc(fl['dimension'])}):</b> {esc(fl['plain'])} Layer {esc(fl['layer'])}; first divergence at {esc(fl['first_divergence'])}; minimised input: {esc(fl['minimised_input'])}; seed {esc(fl['seed'])}.</p>"
    if a.get("repair"):
        out += f"<p><b>Repair:</b> {a['repair']['attempts']} attempts, {esc(a['repair']['outcome'])}.</p>"
    if a.get("defect_notice"):
        out += f"<p><b>Defect notice:</b> {esc(a['defect_notice'])}</p>"
    e = a.get("evidence")
    if e:
        mut = e.get("mutants")
        out += "<p class='muted'>Coverage of the original " + f"{e['coverage_original']:.0%}" + (f", validator coverage {e['validator_coverage']:.0%}" if "validator_coverage" in e else "") + (f"; mutants killed {mut['killed']}, survived {mut['survived']}, not covered {mut['not_covered']}, equivalent {mut['equivalent']}" if mut else "") + f"; tracing {esc(e['tracing'])}; ran on {esc(e['ran_on'])}.</p>"
    if a["constructs"]:
        out += "<table><tr><th>Construct</th><th>Status</th><th>How, or why not</th></tr>"
        for c in a["constructs"]:
            out += f"<tr><td>{esc(c['what'])}</td><td>{esc(c['status'])}</td><td>{esc(c.get('how') or c.get('caveat') or c.get('reason') or c.get('producer') or '')}</td></tr>"
        out += "</table>"
    return out

def eng_registers():
    out = "<h2>Degradations (nothing degrades silently)</h2><table><tr><th>Kind</th><th>Where</th><th>What</th></tr>"
    out += "".join(f"<tr><td>{esc(d['kind'])}</td><td>{esc(d['where'])}</td><td>{esc(d['text'])}</td></tr>" for d in DATA["degradations"]) + "</table>"
    out += "<h2>Changes since the last run</h2><ul>" + "".join(f"<li>{esc(c['artefact'])}: {esc(c['change'])}, because {esc(c['why'])}</li>" for c in DATA["changes_since_last_run"]) + "</ul>"
    out += "<h2>Open problems for a reviewer</h2><ul>" + "".join(f"<li>{esc(p['kind'])}: {esc(p['artefact'])}</li>" for p in DATA["open_problems"]) + "</ul>"
    out += "<h2>Required deployment steps</h2><ol>" + "".join(f"<li>{esc(s)}</li>" for s in DATA["deployment_steps"]) + "</ol>"
    return out

def eng_a():
    body = header_lines(DATA["run"]) + "<h1>Engineering report</h1>" + eng_run_header()
    body += "<h2>Artefacts</h2>" + "".join(eng_artefact(a) for a in DATA["artefacts"]) + eng_registers()
    return page("Engineering (A: artefact by artefact)", body)

# ---------- Engineering report, variant B: register first, artefacts as an appendix ----------
def eng_b():
    rank = {"our_work": 0, "waiting": 1, "manual": 2, "ready": 3}
    rows = sorted(DATA["artefacts"], key=lambda a: (rank[status(a)[1]], a["name"]))
    body = header_lines(DATA["run"]) + "<h1>Engineering report</h1>" + eng_run_header()
    body += "<h2>Everything that is not clean, worst first</h2><table><tr><th>Artefact</th><th>Status</th><th>Reason</th><th>Next step</th></tr>"
    for a in rows:
        s = status(a)
        reason = a.get("failure", {}).get("plain") or a.get("defect_notice") or (f"waits on {a['blocked_by']}" if a.get("blocked_by") else "") or "; ".join(f.get("text", f["kind"]) for f in a["flags"])
        nxt = {"our_work": "reviewer or release", "waiting": "re-verified automatically", "manual": "manual work", "ready": "none"}[s[1]]
        body += f"<tr><td>{esc(a['name'])}</td><td class='{s[1]}'>{esc(s[0])}</td><td>{esc(reason)}</td><td>{esc(nxt)}</td></tr>"
    body += "</table>" + eng_registers()
    body += "<h2>Appendix: every artefact</h2>" + "".join(eng_artefact(a) for a in DATA["artefacts"])
    return page("Engineering (B: register first)", body)

VARIANTS = {
    "signoff": {"A": ("Verdict letter", signoff_a), "B": ("By business area", signoff_b), "C": ("Questions and answers", signoff_c)},
    "engineering": {"A": ("Artefact by artefact", eng_a), "B": ("Register first", eng_b)},
}

VIEWER = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Report prototype</title>
<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%}
#bar{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#111;color:#fff;border-radius:999px;padding:8px 14px;font:13px system-ui;box-shadow:0 4px 14px rgba(0,0,0,.35);display:flex;gap:10px;align-items:center}
#bar button{background:#333;color:#fff;border:0;border-radius:999px;padding:4px 10px;cursor:pointer}</style></head><body>
<iframe id="f"></iframe>
<div id="bar"><button id="doc"></button><button id="prev">&larr;</button><span id="label"></span><button id="next">&rarr;</button><a id="pdf" style="color:#9cf" target="_blank">PDF</a></div>
<script>
const V = __VARIANTS__;
const p = new URLSearchParams(location.search);
let doc = p.get('doc') || 'signoff', keys = () => Object.keys(V[doc]);
let v = p.get('variant') || 'A';
function show(){ if(!V[doc][v]) v = keys()[0];
  document.getElementById('f').src = doc + '-' + v + '.html';
  document.getElementById('label').textContent = (doc==='signoff'?'Sign-off ':'Engineering ') + v + ' (' + V[doc][v] + ')';
  document.getElementById('doc').textContent = 'switch to ' + (doc==='signoff'?'engineering':'sign-off');
  document.getElementById('pdf').href = doc + '-' + v + '.pdf';
  history.replaceState(null,'','?doc='+doc+'&variant='+v); }
function step(d){ const k = keys(); v = k[(k.indexOf(v)+d+k.length)%k.length]; show(); }
document.getElementById('prev').onclick=()=>step(-1); document.getElementById('next').onclick=()=>step(1);
document.getElementById('doc').onclick=()=>{ doc = doc==='signoff'?'engineering':'signoff'; v='A'; show(); };
addEventListener('keydown', e=>{ if(e.key==='ArrowLeft')step(-1); if(e.key==='ArrowRight')step(1); });
show();
</script></body></html>
"""

def main():
    global DATA
    DATA = json.loads((HERE / "report-data.json").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    for doc, vs in VARIANTS.items():
        for key, (_, fn) in vs.items():
            (OUT / f"{doc}-{key}.html").write_text(fn(), encoding="utf-8", newline="\n")
    names = {d: {k: n for k, (n, _) in vs.items()} for d, vs in VARIANTS.items()}
    (OUT / "index.html").write_text(VIEWER.replace("__VARIANTS__", json.dumps(names)), encoding="utf-8", newline="\n")
    print("wrote", sorted(p.name for p in OUT.iterdir()))

if __name__ == "__main__":
    main()
