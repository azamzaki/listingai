"""Local web app: dashboard, OpenAI settings, location campaigns, add listing.

    python -m listingai serve

Runs only on this computer (127.0.0.1). Data is kept in ~/.listingai
(or LISTINGAI_HOME).
"""

from __future__ import annotations

import html
import re
import secrets
import socket
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from .regions import PRESETS, REGIONS
from .campaigns import Campaign, apply_campaigns, load_campaigns, new_campaign, parse_locations, save_campaigns
from .config import DEFAULT_CONFIG
from .dashboard import _TEMPLATE, render_dashboard_html
from .exclusive_agent import classify_exclusive_agent
from .examples import is_example
from .extract import extract_listing, split_posts
from .llm import OpenAIError, classify_with_ai, test_api_key
from .models import EvidenceSource, ExclusiveAgentStatus as S, Listing, TextEvidence
from .settings import Settings, data_dir, load_settings, load_store, save_settings, save_store

_esc = html.escape
_STYLE = re.search(r"<link rel=\"preconnect\".*?</style>", _TEMPLATE, re.S).group(0)
_FORM_CSS = """<style>
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px 22px;display:flex;flex-direction:column;gap:14px}
.card h2{margin:0;font:700 19px/1.25 var(--display)}
.card p{margin:0;color:var(--ink-2);max-width:68ch}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.field{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:600;color:var(--ink-2)}
.field input,.field textarea,.field select{font:15px var(--body);color:var(--ink);padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--paper);width:100%}
.field textarea{min-height:110px;resize:vertical}
.hint{font-weight:400;color:var(--ink-3);font-size:12.5px}
.check{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--ink-2)}
.check input{width:16px;height:16px;accent-color:var(--accent)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.b{display:inline-flex;align-items:center;padding:9px 14px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);font:600 14px var(--body);cursor:pointer;text-decoration:none}
.b:hover{border-color:var(--ink-3)}
.b.primary{background:var(--accent);border-color:var(--accent);color:var(--surface)}
.b.danger{color:var(--rejects);border-color:color-mix(in srgb,var(--rejects) 40%,var(--line))}
.status{display:inline-flex;gap:8px;align-items:center;font-size:14px;font-weight:600}
.status .dot{width:9px;height:9px;border-radius:50%;background:var(--ink-3)}
.status.on .dot{background:var(--good)} .status.on{color:var(--good)}
.camps{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.camp{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:flex;flex-direction:column;gap:10px}
.camp.off{opacity:.65}
.camp h3{margin:0;font:700 17px/1.25 var(--display)}
.places{display:flex;flex-wrap:wrap;gap:6px}
.place{font-size:12.5px;padding:3px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-weight:600}
.stats{display:flex;gap:18px;font-size:13px;color:var(--ink-3)}
.stats b{font:700 18px var(--display);color:var(--ink);display:block;font-variant-numeric:tabular-nums}
.muted{color:var(--ink-3);font-size:13px}
form.inline{display:inline}
</style>"""


def _int(value: str) -> Optional[int]:
    value = (value or "").replace(",", "").replace("RM", "").replace("rm", "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        raise ValueError(f"'{value}' is not a number.") from None


def _valid_phone(phone: str) -> bool:
    return len(re.sub(r"\D", "", phone or "")) >= 9


def _valid_email(email: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or "") is not None


class App:
    def __init__(self, import_csv: Optional[Path] = None):
        from .__main__ import load_listings

        self.lock = threading.Lock()
        self.csrf = secrets.token_urlsafe(24)
        self.base_url = "http://127.0.0.1:8321/"
        self.flash: Optional[tuple[str, bool]] = None
        self.settings: Settings = load_settings()
        self.campaigns: list[Campaign] = load_campaigns()
        stored = load_store() or []
        # Earlier versions loaded example listings on first start; drop them.
        self.removed_examples = sum(1 for l in stored if is_example(l))
        stored = [l for l in stored if not is_example(l)]
        if self.removed_examples:
            self.say(f"Removed {self.removed_examples} example listings. Only your own listings are shown now.")
        if import_csv is not None:
            known = {" ".join(l.caption.lower().split()) for l in stored}
            stored += [l for l in load_listings(import_csv) if " ".join(l.caption.lower().split()) not in known]
        self.listings: list[Listing] = stored
        self.refresh()

    # --- state ------------------------------------------------------------
    def refresh(self) -> None:
        apply_campaigns(self.listings, self.campaigns)
        save_store(self.listings)

    def classify(self, listing: Listing, use_ai: bool) -> None:
        if not listing.public_contact:
            listing.exclusive_agent = None
            return
        now = datetime.now(timezone.utc)
        key = self.settings.effective_key
        if use_ai and key:
            listing.exclusive_agent = classify_with_ai(listing.all_evidence(), key, self.settings.openai_model, DEFAULT_CONFIG, now)
        else:
            listing.exclusive_agent = classify_exclusive_agent(listing.all_evidence(), DEFAULT_CONFIG, now)

    def say(self, text: str, error: bool = False) -> None:
        self.flash = (text, error)

    def take_flash(self) -> str:
        if not self.flash:
            return ""
        text, err = self.flash
        self.flash = None
        return f'<div class="flash{" err" if err else ""}" role="status">{_esc(text)}</div>'

    # --- pages ------------------------------------------------------------
    def nav(self, current: str) -> str:
        links = [("/", "Dashboard"), ("/add", "Add listing"), ("/import", "Import posts"), ("/campaigns", "Campaigns"), ("/settings", "Settings")]
        out = "".join(
            f'<a href="{href}"{" class=primary" if href == current else ""}>{label}</a>' for href, label in links
        )
        if current == "/" and self.settings.effective_key and self.listings:
            out += (f'<form method="post" action="/reanalyse"><input type="hidden" name="csrf" value="{self.csrf}">'
                    f'<button type="submit">Re-check all with AI</button></form>')
        ai = "AI on" if self.settings.effective_key else "AI off (phrase rules only)"
        return f'<nav class="appnav" aria-label="Main">{out}<span class="muted" style="align-self:center">{ai}</span></nav>'

    def shell(self, title: str, current: str, body: str) -> str:
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'</head><body>{_STYLE.replace("<title>ListingAI Lead Desk</title>", "")}{_FORM_CSS}'
                f'<title>{_esc(title)} · ListingAI</title>'
                f'<div class="wrap"><header class="head"><div class="brand"><span class="eyebrow">ListingAI</span>'
                f'<h1>{_esc(title)}</h1>{self.nav(current)}</div></header>{self.take_flash()}{body}</div></body></html>')

    def dashboard(self) -> str:
        names = {c.id: c.name for c in self.campaigns}
        return render_dashboard_html(self.listings, DEFAULT_CONFIG, source_name="your ListingAI data",
                                     nav_html=self.nav("/"), flash_html=self.take_flash(), campaign_names=names)

    def settings_page(self) -> str:
        s = self.settings
        from_env = not s.openai_api_key and bool(s.effective_key)
        status = (f'<span class="status on"><span class="dot"></span>Key saved ({_esc(s.key_hint)})'
                  f'{" from OPENAI_API_KEY" if from_env else ""}</span>' if s.effective_key
                  else '<span class="status"><span class="dot"></span>No key saved</span>')
        c = self.csrf
        remove = (f'<form class="inline" method="post" action="/settings/remove"><input type="hidden" name="csrf" value="{c}">'
                  f'<button class="b danger" type="submit">Remove key</button></form>') if s.openai_api_key else ""
        test = (f'<form class="inline" method="post" action="/settings/test"><input type="hidden" name="csrf" value="{c}">'
                f'<button class="b" type="submit">Test connection</button></form>') if s.effective_key else ""
        body = f"""
<section class="card">
  <h2>OpenAI connection</h2>
  <p>With a key, each post is also read by an OpenAI model to catch wording the phrase rules miss. The AI must quote the
  post word for word, and if it disagrees with the rules the listing goes to the review queue. The key is stored only on
  this computer, in <code>{_esc(str(data_dir() / 'settings.json'))}</code> (or your LISTINGAI_HOME folder).</p>
  <div class="row">{status}</div>
  <form method="post" action="/settings" class="grid2" autocomplete="off">
    <input type="hidden" name="csrf" value="{c}">
    <label class="field" for="api_key">OpenAI API key
      <input id="api_key" name="api_key" type="password" placeholder="{'Leave blank to keep the saved key' if s.openai_api_key else 'sk-…'}" spellcheck="false">
      <span class="hint">Create one at platform.openai.com → API keys.</span></label>
    <label class="field" for="model">Model
      <input id="model" name="model" type="text" value="{_esc(s.openai_model)}" spellcheck="false">
      <span class="hint">Any chat model your OpenAI account can use.</span></label>
    <div class="row" style="grid-column:1/-1"><button class="b primary" type="submit">Save settings</button></div>
  </form>
  <div class="row">{test}{remove}</div>
</section>"""
        return self.shell("Settings", "/settings", body)

    def campaigns_page(self, edit_id: str = "") -> str:
        c = self.csrf
        editing = next((x for x in self.campaigns if x.id == edit_id), None)
        cards = []
        for camp in self.campaigns:
            matched = [l for l in self.listings if camp.id in l.campaign_ids]
            seeking = sum(1 for l in matched if l.exclusive_agent and l.exclusive_agent.exclusive_agent_status is S.SEEKING_EXCLUSIVE_AGENT)
            price = "Any price"
            if camp.min_price is not None or camp.max_price is not None:
                lo = f"RM{camp.min_price:,}" if camp.min_price is not None else "any"
                hi = f"RM{camp.max_price:,}" if camp.max_price is not None else "any"
                price = f"{lo} – {hi}"
            places = "".join(f'<span class="place">{_esc(p)}</span>' for p in camp.locations)
            cards.append(f"""
<article class="camp{'' if camp.active else ' off'}">
  <div class="row" style="justify-content:space-between"><h3>{_esc(camp.name)}</h3>
    <span class="status{' on' if camp.active else ''}"><span class="dot"></span>{'Active' if camp.active else 'Paused'}</span></div>
  <div class="places">{places}</div>
  <div class="muted">{_esc(price)}{' · alerts to ' + _esc(camp.alert_email) if camp.alert_email else ''}</div>
  <div class="stats"><span><b>{len(matched)}</b>listings</span><span><b>{seeking}</b>seeking an agent</span></div>
  <div class="row">
    <a class="b" href="/?campaign={_esc(quote(camp.name))}">View listings</a>
    <a class="b" href="/campaigns?edit={_esc(camp.id)}">Edit</a>
    <form class="inline" method="post" action="/campaigns/toggle"><input type="hidden" name="csrf" value="{c}"><input type="hidden" name="id" value="{_esc(camp.id)}">
      <button class="b" type="submit">{'Pause' if camp.active else 'Resume'}</button></form>
    <form class="inline" method="post" action="/campaigns/delete" onsubmit="return confirm('Delete campaign {_esc(camp.name)}?')"><input type="hidden" name="csrf" value="{c}"><input type="hidden" name="id" value="{_esc(camp.id)}">
      <button class="b danger" type="submit">Delete</button></form>
  </div>
</article>""")
        listing = "".join(cards) or '<p class="muted">No campaigns yet. Until you create one, every listing counts as in a target location.</p>'
        e = editing
        presets = "".join(
            f'<button type="button" class="b preset" data-name="{_esc(name)}" data-places="{_esc(", ".join(REGIONS[name]))}">+ {_esc(name)}</button>'
            for name in PRESETS
        )
        form = f"""
<section class="card">
  <h2>{'Edit campaign' if e else 'New campaign'}</h2>
  <p>Pick the areas you work in. A listing joins the campaign when its location or post text mentions one of these places
  and its price is in range. Only listings in an active campaign can trigger exclusive-opportunity alerts.</p>
  <div class="row" aria-label="Region presets"><span class="muted">Quick fill:</span>{presets}</div>
  <form method="post" action="/campaigns/save" class="grid2">
    <input type="hidden" name="csrf" value="{c}"><input type="hidden" name="id" value="{_esc(e.id) if e else ''}">
    <label class="field" for="c_name">Campaign name
      <input id="c_name" name="name" required value="{_esc(e.name) if e else ''}" placeholder="Penang mainland landed"></label>
    <label class="field" for="c_email"><span>Alert email <span class="hint">· optional</span></span>
      <input id="c_email" name="alert_email" type="email" value="{_esc(e.alert_email) if e else ''}" placeholder="you@example.com"></label>
    <label class="field" for="c_locs" style="grid-column:1/-1">Locations
      <textarea id="c_locs" name="locations" required placeholder="Bukit Mertajam, Butterworth, Seberang Jaya, Sungai Petani">{_esc(', '.join(e.locations)) if e else ''}</textarea>
      <span class="hint">Separate places with commas or new lines. Short forms like "Sg Petani", "Bkt Mertajam" and "Alor Star" are matched automatically. Add housing areas owners mention, e.g. "Taman Ria Jaya".</span></label>
    <label class="field" for="c_min"><span>Minimum price (RM) <span class="hint">· optional</span></span>
      <input id="c_min" name="min_price" inputmode="numeric" value="{e.min_price if e and e.min_price is not None else ''}" placeholder="300000"></label>
    <label class="field" for="c_max"><span>Maximum price (RM) <span class="hint">· optional</span></span>
      <input id="c_max" name="max_price" inputmode="numeric" value="{e.max_price if e and e.max_price is not None else ''}" placeholder="900000"></label>
    <label class="check" for="c_active"><input id="c_active" name="active" type="checkbox" {'checked' if (e is None or e.active) else ''}> Active</label>
    <div class="row" style="grid-column:1/-1"><button class="b primary" type="submit">{'Save changes' if e else 'Create campaign'}</button>
      {'<a class="b" href="/campaigns">Cancel</a>' if e else ''}</div>
  </form>
</section>"""
        script = """<script>
document.querySelectorAll('.preset').forEach(function(b){b.addEventListener('click',function(){
  var box=document.getElementById('c_locs'), name=document.getElementById('c_name');
  var have=box.value.split(/[,\\n;]+/).map(function(x){return x.trim()}).filter(Boolean);
  var lower=have.map(function(x){return x.toLowerCase()});
  b.dataset.places.split(', ').forEach(function(p){ if(lower.indexOf(p.toLowerCase())<0){ have.push(p); lower.push(p.toLowerCase()); } });
  box.value=have.join(', ');
  if(!name.value.trim()) name.value=b.dataset.name;
});});
</script>"""
        return self.shell("Campaigns", "/campaigns", f'{form}<section class="camps">{listing}</section>{script}')

    def add_page(self, prefill: Optional[dict[str, str]] = None) -> str:
        c = self.csrf
        pre = prefill or {}
        today = datetime.now().strftime("%Y-%m-%d")
        ai = "OpenAI and the phrase rules" if self.settings.effective_key else "the phrase rules (add an OpenAI key in Settings for AI checking)"
        body = f"""
<section class="card">
  <h2>Paste a post</h2>
  <p>Leave the details blank and they are read from the post: phone, email, price, location and whether the owner
  posted it. The listing is then checked by {ai} and added to the dashboard.</p>
  <form method="post" action="/add" class="grid2">
    <input type="hidden" name="csrf" value="{c}">
    <label class="field" for="a_caption" style="grid-column:1/-1">Post text
      <textarea id="a_caption" name="caption" required placeholder="Owner jual rumah teres Bukit Mertajam RM450k. Nak lantik seorang ejen sahaja. WhatsApp 012-3456789">{_esc(pre.get("caption", ""))}</textarea></label>
    <label class="field" for="a_ocr" style="grid-column:1/-1">Text in the photos <span class="hint">optional, typed exactly as shown</span>
      <textarea id="a_ocr" name="image_text" style="min-height:60px"></textarea></label>
    <label class="field" for="a_comment" style="grid-column:1/-1">Owner's comment <span class="hint">optional, only comments written by the owner</span>
      <textarea id="a_comment" name="owner_comment" style="min-height:60px"></textarea></label>
    <label class="field" for="a_loc"><span>Location <span class="hint">· blank = detect</span></span><input id="a_loc" name="location" placeholder="Sungai Petani"></label>
    <label class="field" for="a_price"><span>Price (RM) <span class="hint">· blank = detect</span></span><input id="a_price" name="price" inputmode="numeric" placeholder="650000"></label>
    <label class="field" for="a_phone"><span>Public phone <span class="hint">· blank = detect</span></span><input id="a_phone" name="phone" inputmode="tel" placeholder="012-3456789"></label>
    <label class="field" for="a_email"><span>Public email <span class="hint">· blank = detect</span></span><input id="a_email" name="email" type="email"></label>
    <label class="field" for="a_url">Link to post<input id="a_url" name="url" type="url" value="{_esc(pre.get("url", ""))}" placeholder="https://facebook.com/…"></label>
    <label class="field" for="a_date">Posted on<input id="a_date" name="posted" type="date" value="{today}"></label>
    <label class="field" for="a_owner_sel">Posted by the owner?
      <select id="a_owner_sel" name="owner"><option value="auto">Detect from the post</option><option value="yes">Yes</option><option value="no">No</option></select></label>
    <label class="field" for="a_score"><span>Base score (0–100) <span class="hint">· your rating before agent intent</span></span>
      <input id="a_score" name="base_score" inputmode="numeric" value="70"></label>
    <label class="field" for="a_scam">Scam risk (0–100)<input id="a_scam" name="scam_risk" inputmode="numeric" value="0"></label>
    <div class="row" style="grid-column:1/-1"><button class="b primary" type="submit">Check and add</button></div>
  </form>
</section>"""
        return self.shell("Add listing", "/add", body)

    def import_page(self) -> str:
        c = self.csrf
        base = self.base_url.rstrip("/")
        bookmarklet = (
            "javascript:(function(){var t=String(getSelection()).trim();"
            "if(!t){alert('Select the text of a listing post first, then click the ListingAI button.');return;}"
            f"window.open('{base}/add?caption='+encodeURIComponent(t.slice(0,6000))+'&url='+encodeURIComponent(location.href),'_blank');}})();"
        )
        body = f"""
<section class="card">
  <h2>Capture posts while you browse</h2>
  <p>Drag this button to your browser's bookmarks bar (press Ctrl+Shift+B if the bar is hidden):</p>
  <div class="row"><a class="b primary" href="{_esc(bookmarklet)}" onclick="event.preventDefault();alert('Drag this button to your bookmarks bar, don\'t click it here.')">+ ListingAI</a></div>
  <p>Then, on Facebook groups, Marketplace, Mudah, Telegram Web or any other site: select the text of a listing post
  with your mouse and click <b>+ ListingAI</b> in the bookmarks bar. ListingAI opens with the post filled in; click
  <b>Check and add</b>. Keep ListingAI running while you browse.</p>
  <p class="muted">You choose each post yourself. ListingAI never logs in to or scans Facebook or other sites for you.</p>
</section>
<section class="card">
  <h2>Import many posts at once</h2>
  <p>Paste posts from WhatsApp, Telegram, Facebook or anywhere else. Put a line with <code>---</code> between posts.
  Details are read from each post, and posts already in ListingAI are skipped.</p>
  <form method="post" action="/import" class="grid2">
    <input type="hidden" name="csrf" value="{c}">
    <label class="field" for="i_posts" style="grid-column:1/-1">Posts
      <textarea id="i_posts" name="posts" required style="min-height:260px" placeholder="Rumah teres Sg Petani RM380k. Owner jual, nak lantik seorang ejen sahaja. 012-3456789&#10;---&#10;Condo Bayan Lepas RM520,000, agents welcome. Call 013-2223344"></textarea></label>
    <label class="field" for="i_loc"><span>Location if a post doesn't say <span class="hint">· optional</span></span><input id="i_loc" name="default_location" placeholder="Kulim"></label>
    <label class="field" for="i_date">Posted on<input id="i_date" name="posted" type="date" value="{datetime.now().strftime("%Y-%m-%d")}"></label>
    <div class="row" style="grid-column:1/-1"><button class="b primary" type="submit">Import posts</button>
      <span class="muted">Up to 100 posts at a time.{" Each post uses a little OpenAI credit." if self.settings.effective_key else ""}</span></div>
  </form>
</section>"""
        return self.shell("Import posts", "/import", body)

    # --- actions ----------------------------------------------------------
    def post(self, path: str, form: dict[str, str]) -> str:
        """Handle a form post; returns the path to redirect to."""
        if path == "/settings":
            key = form.get("api_key", "").strip()
            model = form.get("model", "").strip() or self.settings.openai_model
            if key and not key.startswith("sk-"):
                self.say("That doesn't look like an OpenAI key. Keys start with sk-.", True)
                return "/settings"
            if key:
                self.settings.openai_api_key = key
            self.settings.openai_model = model
            save_settings(self.settings)
            if key:
                try:
                    self.say("Key saved. " + test_api_key(self.settings.effective_key, model))
                except OpenAIError as e:
                    self.say(f"Key saved, but the test failed: {e}", True)
            else:
                self.say("Settings saved.")
            return "/settings"
        if path == "/settings/test":
            try:
                self.say(test_api_key(self.settings.effective_key, self.settings.openai_model))
            except OpenAIError as e:
                self.say(str(e), True)
            return "/settings"
        if path == "/settings/remove":
            self.settings.openai_api_key = ""
            save_settings(self.settings)
            self.say("API key removed from this computer.")
            return "/settings"
        if path == "/campaigns/save":
            try:
                camp = new_campaign(form.get("name", ""), parse_locations(form.get("locations", "")),
                                    _int(form.get("min_price", "")), _int(form.get("max_price", "")),
                                    form.get("alert_email", ""), "active" in form)
            except ValueError as e:
                self.say(str(e), True)
                return "/campaigns"
            existing = next((x for x in self.campaigns if x.id == form.get("id")), None)
            if existing:
                camp.id, camp.created_at = existing.id, existing.created_at
                self.campaigns[self.campaigns.index(existing)] = camp
                self.say(f"Campaign “{camp.name}” updated.")
            else:
                self.campaigns.append(camp)
                self.say(f"Campaign “{camp.name}” created.")
            save_campaigns(self.campaigns)
            self.refresh()
            return "/campaigns"
        if path in ("/campaigns/toggle", "/campaigns/delete"):
            camp = next((x for x in self.campaigns if x.id == form.get("id")), None)
            if camp:
                if path.endswith("toggle"):
                    camp.active = not camp.active
                    self.say(f"Campaign “{camp.name}” {'resumed' if camp.active else 'paused'}.")
                else:
                    self.campaigns.remove(camp)
                    self.say(f"Campaign “{camp.name}” deleted.")
                save_campaigns(self.campaigns)
                self.refresh()
            return "/campaigns"
        if path == "/add":
            return self._add(form)
        if path == "/import":
            return self._import(form)
        if path == "/reanalyse":
            errors = 0
            for l in self.listings:
                self.classify(l, use_ai=True)
                if l.exclusive_agent and "AI unavailable" in (l.exclusive_agent.review_reason or ""):
                    errors += 1
            self.refresh()
            if errors:
                self.say(f"Re-checked {len(self.listings)} listings. The AI could not be reached for {errors}; those kept the phrase-rule result.", True)
            else:
                self.say(f"Re-checked {len(self.listings)} listings with AI.")
            return "/"
        return "/"

    def _is_duplicate(self, caption: str) -> bool:
        key = " ".join(caption.lower().split())
        return any(" ".join(l.caption.lower().split()) == key for l in self.listings)

    def _build(self, caption: str, form: dict[str, str], extra_evidence: list[TextEvidence]) -> Listing:
        """Create a listing, reading any detail the form left blank from the post text."""
        phone, email = form.get("phone", "").strip(), form.get("email", "").strip()
        if phone and not _valid_phone(phone):
            raise ValueError("The phone number needs at least 9 digits.")
        if email and not _valid_email(email):
            raise ValueError("The email address doesn't look right.")
        price = _int(form.get("price", ""))
        base = max(0, min(100, _int(form.get("base_score", "")) or 70))
        scam = max(0, min(100, _int(form.get("scam_risk", "")) or 0))
        posted = datetime.strptime(form["posted"], "%Y-%m-%d").replace(tzinfo=timezone.utc) if form.get("posted") else None
        location = form.get("location", "").strip()
        owner_choice = form.get("owner", "auto")

        full_text = "\n".join([caption] + [e.text for e in extra_evidence])
        places = [p for c in self.campaigns for p in c.locations]
        found = extract_listing(full_text, places, self.settings.effective_key, self.settings.openai_model)
        phone = phone or found.phone or ""
        email = email or found.email or ""
        location = location or found.location or form.get("default_location", "").strip()
        owner = {"yes": True, "no": False}.get(owner_choice, found.is_direct_owner or False)
        return Listing(
            id=f"M{datetime.now().strftime('%y%m%d%H%M%S')}{secrets.token_hex(2)}",
            post_url=form.get("url", "").strip(), location=location, price=price if price is not None else found.price,
            caption=caption, is_direct_owner=owner, public_phone=phone or None,
            public_email=email or None, has_eligible_contact=bool(phone or email), matches_target_location=True,
            posted_at=posted, scam_risk_score=scam, base_opportunity_score=base,
            evidence=[TextEvidence(EvidenceSource.CAPTION, caption)] + extra_evidence,
        )

    def _add(self, form: dict[str, str]) -> str:
        caption = form.get("caption", "").strip()
        extra = []
        if form.get("image_text", "").strip():
            extra.append(TextEvidence(EvidenceSource.IMAGE_OCR, form["image_text"].strip()))
        if form.get("owner_comment", "").strip():
            extra.append(TextEvidence(EvidenceSource.COMMENT, form["owner_comment"].strip()))
        try:
            if not caption:
                raise ValueError("Paste the post text.")
            if self._is_duplicate(caption):
                raise ValueError("This post is already in ListingAI.")
            listing = self._build(caption, form, extra)
        except ValueError as e:
            self.say(str(e), True)
            return "/add"
        self.classify(listing, use_ai=True)
        self.listings.append(listing)
        self.refresh()
        found = ", ".join(x for x in [listing.location, f"RM{listing.price:,}" if listing.price else "", listing.public_contact or ""] if x)
        if listing.exclusive_agent is None:
            self.say(f"Added ({found or 'no details found'}). No public phone or email was found, so agent intent was not checked.")
        else:
            ea = listing.exclusive_agent
            quote = f": “{ea.exclusive_agent_evidence}”" if ea.exclusive_agent_evidence else ""
            self.say(f"Added ({found}) as {ea.exclusive_agent_status.value.replace('_', ' ')}{quote}"
                     + (" (checked with AI)" if "openai" in ea.classified_by else ""))
        return "/"

    def _import(self, form: dict[str, str]) -> str:
        posts = split_posts(form.get("posts", ""))
        if not posts:
            self.say("Paste at least one post.", True)
            return "/import"
        if len(posts) > 100:
            self.say(f"That's {len(posts)} posts. Import up to 100 at a time.", True)
            return "/import"
        added = dupes = no_contact = 0
        for text in posts:
            if self._is_duplicate(text):
                dupes += 1
                continue
            try:
                listing = self._build(text, {"posted": form.get("posted", ""),
                                             "default_location": form.get("default_location", "")}, [])
            except ValueError:
                continue
            self.classify(listing, use_ai=True)
            self.listings.append(listing)
            added += 1
            if not listing.public_contact:
                no_contact += 1
        self.refresh()
        parts = [f"Imported {added} of {len(posts)} posts."]
        if dupes:
            parts.append(f"{dupes} already in ListingAI.")
        if no_contact:
            parts.append(f"{no_contact} had no public phone or email, so agent intent was not checked.")
        self.say(" ".join(parts))
        return "/"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ListingAI"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _local_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body: str = "", location: str = "") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._local_host():
                return self._send(403, "Forbidden")
            url = urlparse(self.path)
            q = parse_qs(url.query)
            with app.lock:
                if url.path == "/":
                    return self._send(200, app.dashboard())
                if url.path == "/settings":
                    return self._send(200, app.settings_page())
                if url.path == "/campaigns":
                    return self._send(200, app.campaigns_page(q.get("edit", [""])[0]))
                if url.path == "/add":
                    return self._send(200, app.add_page({k: v[0] for k, v in q.items()}))
                if url.path == "/import":
                    return self._send(200, app.import_page())
            self._send(404, "Not found")

        def do_POST(self):
            if not self._local_host():
                return self._send(403, "Forbidden")
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            raw = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            form = {k: v[0] for k, v in raw.items()}
            if not secrets.compare_digest(form.get("csrf", ""), app.csrf):
                return self._send(403, "This form has expired. Go back and reload the page.")
            with app.lock:
                target = app.post(urlparse(self.path).path, form)
            self._send(303, location=target)

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second program bind a port that is already
    # in use, so the browser may reach the other program. Use exclusive binding.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def port_answers(port: int) -> bool:
    """True if any program already accepts connections on this port (IPv4 or IPv6 localhost)."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False


def bind_server(app: "App", port: int, attempts: int = 20) -> ThreadingHTTPServer:
    """Bind to `port`, or the next port that no other program is using."""
    last: Optional[OSError] = None
    for candidate in range(port, port + attempts):
        if port_answers(candidate):
            continue
        try:
            return _Server(("127.0.0.1", candidate), make_handler(app))
        except OSError as e:
            last = e
    raise OSError(f"No free port between {port} and {port + attempts - 1}: {last}")


def serve(port: int = 8321, import_csv: Optional[Path] = None, open_browser: bool = True) -> None:
    app = App(import_csv)
    if app.removed_examples:
        print(f"Removed {app.removed_examples} example listings.")
    httpd = bind_server(app, port)
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/"
    app.base_url = url
    if actual != port:
        print(f"Port {port} is used by another program, so ListingAI is using port {actual} instead.")
    print(f"ListingAI is running at {url}\nPress Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
