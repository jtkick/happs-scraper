// Case editor: labels on the right, the page and its text on the left.
// The model is `labels` (what gets saved); `state` is the server's latest
// parse and comparison, refreshed on load, save and re-run.
(() => {
  const R = window.REVIEW;
  const api = `/api/case/${R.where}/${encodeURIComponent(R.id)}`;

  const LIST = new Set(['tag_names', 'recurrence_byday', 'rdates']);
  const JSONISH = new Set(['exdates']);
  const NUMBER = new Set(['location_lat', 'location_lon', 'ticket_price', 'recurrence_interval', 'recurrence_count']);
  const LONG = new Set(['description', 'evidence']);
  const DATETIME = new Set(['start_datetime', 'end_datetime']);
  // Values every parsed event carries unless it recurs; not worth a row.
  const DEFAULTS = {recurrence_freq: 'none', recurrence_interval: 1, recurrence_month_mode: 'day'};
  const CORE = ['title', 'start_datetime', 'end_datetime', 'description', 'location_title',
                'location_address', 'ticket_price', 'url'];

  let state = null;
  let labels = null;
  let dirty = false;
  let selection = '';
  const expanded = new Set();

  // ── DOM helper ──────────────────────────────────────────────────────────────
  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'class') el.className = v;
      else if (k === 'value') el.value = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  // ── Server calls ────────────────────────────────────────────────────────────
  async function call(method, url, body) {
    const resp = await fetch(url, {
      method, headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
    return data;
  }

  function payload() {
    return {
      events: labels.events, not_events: labels.not_events, complete: labels.complete,
      notes: labels.notes, kind: labels.kind, seed_context: labels.seed_context,
      partial: labels.partial, known_failures: labels.known_failures,
    };
  }

  function adopt(data, keepLabels = false) {
    state = data;
    if (!keepLabels) {
      const c = data.case;
      labels = {
        events: structuredClone(c.events), not_events: [...c.not_events], complete: c.complete,
        notes: c.notes || '', kind: c.kind, seed_context: structuredClone(c.seed_context || {}),
        partial: c.partial ? structuredClone(c.partial) : null,
        known_failures: structuredClone(c.known_failures || []),
      };
      setDirty(false);
    }
    render();
  }

  async function busy(button, label, fn) {
    const old = button.textContent;
    button.disabled = true; button.textContent = label;
    try { await fn(); } catch (e) { alert(e.message); } finally {
      button.textContent = old; button.disabled = button.id === 'save' ? !dirty : false;
    }
  }

  async function save() {
    const btn = document.getElementById('save');
    await busy(btn, 'Saving…', async () => adopt(await call('PUT', api, payload())));
  }

  async function rerun(live) {
    const btn = document.getElementById(live ? 'rerun-live' : 'rerun');
    await busy(btn, live ? 'Asking the model…' : 'Parsing…', async () => {
      adopt(await call('POST', `${api}/rerun`, {labels: payload(), live}), true);
    });
  }

  function setDirty(value) {
    dirty = value;
    const btn = document.getElementById('save');
    if (btn) btn.disabled = !dirty;
    document.title = (dirty ? '• ' : '') + document.title.replace(/^• /, '');
  }

  function changed() { setDirty(true); }

  // ── Value formatting ────────────────────────────────────────────────────────
  function toText(field, value) {
    if (value === null || value === undefined) return '';
    if (LIST.has(field)) return value.join(', ');
    if (JSONISH.has(field)) return JSON.stringify(value);
    return String(value);
  }

  function fromText(field, text) {
    text = text.trim();
    if (LIST.has(field)) return text ? text.split(',').map(s => s.trim()).filter(Boolean) : [];
    if (JSONISH.has(field)) { try { return text ? JSON.parse(text) : []; } catch { return text; } }
    if (NUMBER.has(field)) return text === '' ? '' : Number(text);
    return text;
  }

  function localTime(value) {
    const tz = labels.seed_context.timezone;
    if (!value || !/T\d\d:\d\d/.test(value)) return '';
    const hasZone = /([+-]\d\d:?\d\d|Z)$/.test(value);
    if (!hasZone) return tz ? `as ${tz}` : 'no offset: read as UTC';
    try {
      return new Intl.DateTimeFormat(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
        timeZone: tz || undefined, timeZoneName: 'short',
      }).format(new Date(value));
    } catch { return ''; }
  }

  // ── Render ──────────────────────────────────────────────────────────────────
  function render() {
    renderStats();
    const root = document.getElementById('editor');
    const scroll = root.scrollTop;
    root.replaceChildren(
      notices(),
      section('Events', `${labels.events.length} labelled`, [
        ...labels.events.map((_, i) => eventCard(i)),
        h('button', {class: 'btn add', onclick: () => { labels.events.push({title: ''}); expanded.add(labels.events.length - 1); changed(); render(); }}, '+ Add an event the parser missed'),
      ]),
      extrasSection(),
      droppedSection(),
      notEventsSection(),
      failuresSection(),
      settingsSection(),
    );
    root.scrollTop = scroll;
  }

  function renderStats() {
    const c = state.comparison;
    const pct = v => `${Math.round(v * 100)}%`;
    const el = document.getElementById('stats');
    el.replaceChildren(...[
      h('span', {class: 'stat'}, 'recall ', h('b', {}, pct(c.recall))),
      h('span', {class: 'stat'}, 'precision ', h('b', {}, pct(c.precision))),
      h('span', {class: `stat ${c.failures.length ? 'bad' : 'good'}`}, h('b', {}, c.failures.length), ' mismatches'),
      state.current.strategy ? h('span', {class: 'tag'}, state.current.strategy) : null,
      state.current.ai_used ? h('span', {class: 'tag ai'}, 'AI') : null,
    ].filter(Boolean));
  }

  function notices() {
    const cur = state.current;
    const out = h('div', {class: 'notices'});
    if (cur.ai_missing) out.append(h('p', {class: 'notice warn'}, 'The parser asked the model, but this case has no recorded answer. Use “Re-run with live AI” to record one.'));
    if (cur.ai_stale) out.append(h('p', {class: 'notice'}, 'The AI prompt has changed since this answer was recorded. Re-run with live AI to refresh it.'));
    if (state.comparison.fixed.length) out.append(h('p', {class: 'notice good'}, `Now passing: ${state.comparison.fixed.join(', ')}. Saving drops them from known failures.`));
    return out;
  }

  function section(title, sub, children) {
    return h('section', {class: 'block'}, h('h2', {}, title, sub ? h('span', {class: 'sub'}, ` ${sub}`) : null), ...children);
  }

  function eventCard(i) {
    const label = labels.events[i];
    const match = state.comparison.matches[i];
    const actual = match && match.actual !== null && match.actual !== undefined ? state.current.events[match.actual] : null;
    const bad = match ? Object.values(match.fields).filter(ok => !ok).length : 0;
    let status;
    if (!match) status = h('span', {class: 'badge'}, 'unsaved');
    else if (!actual) status = h('span', {class: 'badge bad'}, 'not parsed');
    else if (bad) status = h('span', {class: 'badge bad'}, `${bad} wrong`);
    else status = h('span', {class: 'badge good'}, 'matches');

    const open = expanded.has(i);
    const shown = R.fields.filter(f => open || CORE.includes(f) || f in label
      || (actual && hasValue(actual[f]) && DEFAULTS[f] !== actual[f]));

    return h('article', {class: `card ${actual ? '' : 'missing'}`},
      h('header', {},
        h('span', {class: 'idx'}, `#${i}`),
        h('span', {class: 'title'}, label.title || '(untitled)'),
        status,
        actual ? h('span', {class: 'sub'}, actual.extraction_method || '', actual.confidence !== undefined ? ` · ${actual.confidence}` : '') : null,
        h('span', {class: 'spacer'}),
        h('button', {class: 'link', onclick: () => { open ? expanded.delete(i) : expanded.add(i); render(); }}, open ? 'Fewer fields' : 'All fields'),
        h('button', {class: 'link danger', title: 'Remove this label', onclick: () => {
          if (actual && confirm('Mark the parsed event as “not an event” too?')) labels.not_events.push(actual.title);
          labels.events.splice(i, 1); changed(); render();
        }}, 'Remove'),
      ),
      h('div', {class: 'fields'}, shown.map(f => fieldRow(i, f, actual, match))),
    );
  }

  function hasValue(v) {
    return !(v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length));
  }

  function fieldRow(i, field, actual, match) {
    const label = labels.events[i];
    const mode = !(field in label) ? 'ignore' : label[field] === null ? 'empty' : 'value';
    const result = match && field in match.fields ? (match.fields[field] ? 'ok' : 'wrong') : '';
    const parsedValue = actual ? actual[field] : undefined;

    const setMode = m => {
      if (m === 'ignore') delete label[field];
      else if (m === 'empty') label[field] = null;
      else label[field] = hasValue(parsedValue) ? structuredClone(parsedValue) : (LIST.has(field) || JSONISH.has(field) ? [] : '');
      changed(); render();
    };

    const input = LONG.has(field) || JSONISH.has(field)
      ? h('textarea', {rows: field === 'description' ? 3 : 1, value: toText(field, label[field]), disabled: mode !== 'value'})
      : h('input', {value: toText(field, label[field]), disabled: mode !== 'value',
                    inputmode: NUMBER.has(field) ? 'decimal' : null,
                    placeholder: DATETIME.has(field) ? '2026-10-01T19:00:00-04:00' : null});
    input.addEventListener('input', () => { label[field] = fromText(field, input.value); changed(); hint.textContent = DATETIME.has(field) ? localTime(label[field]) : ''; });
    if (field === 'title') input.addEventListener('change', render);
    const hint = h('span', {class: 'local'}, DATETIME.has(field) && mode === 'value' ? localTime(label[field]) : '');

    const paste = h('button', {class: 'paste', title: 'Copy the text selected on the left', onclick: () => {
      if (!selection) return;
      label[field] = fromText(field, selection); changed(); render();
    }}, '⇐');

    const seg = h('span', {class: 'seg'},
      ...[['value', '✓', 'Assert this value'], ['empty', '∅', 'Assert the field is empty'], ['ignore', '–', "Don't check this field"]]
        .map(([m, text, title]) => h('button', {class: mode === m ? 'on' : '', title, onclick: () => setMode(m)}, text)));

    // The parser's value matters when it's wrong or this field isn't asserted.
    const showParsed = actual && !(mode === 'value' && result === 'ok');
    return h('div', {class: `field ${mode} ${result}`},
      h('label', {}, field),
      seg,
      h('div', {class: 'value'}, input, hint),
      paste,
      h('div', {class: 'parsed', title: 'What the parser produced'},
        showParsed ? h('span', {}, h('span', {class: 'parsed-label'}, 'parsed: '),
          hasValue(parsedValue) ? toText(field, parsedValue) : h('i', {}, 'empty'),
          DATETIME.has(field) && hasValue(parsedValue) ? h('span', {class: 'local'}, ' ', localTime(parsedValue)) : null) : ''),
    );
  }

  function extrasSection() {
    const c = state.comparison;
    const rows = [...c.extras.map(j => [j, 'extra']), ...c.rejected.map(j => [j, 'rejected'])];
    if (!rows.length) return '';
    return section('Parsed but not labelled', labels.complete ? 'each counts as a wrong event' : 'ignored: labels are marked incomplete',
      rows.map(([j, kind]) => {
        const e = state.current.events[j];
        return h('div', {class: `row-item ${kind}`},
          h('span', {class: 'title'}, e.title),
          h('span', {class: 'sub'}, e.start_datetime || '', ' · ', e.extraction_method || ''),
          h('span', {class: 'spacer'}),
          kind === 'rejected' ? h('span', {class: 'badge bad'}, 'listed as not an event')
            : h('button', {class: 'btn small', onclick: () => { labels.not_events.push(e.title); changed(); render(); }}, 'Not an event'),
          h('button', {class: 'btn small', onclick: () => { labels.events.push(structuredClone(state.current.labels[j])); changed(); render(); }}, 'Add as event'),
        );
      }));
  }

  function droppedSection() {
    const dropped = state.current.dropped;
    if (!dropped.length) return '';
    return section('Dropped by validation', `${dropped.length}`, dropped.map((e, j) =>
      h('div', {class: 'row-item'},
        h('span', {class: 'title'}, e.title || '(untitled)'),
        h('span', {class: 'badge'}, e.drop_reason),
        h('span', {class: 'sub'}, e.start_datetime || ''),
        h('span', {class: 'spacer'}),
        h('button', {class: 'btn small', onclick: () => { labels.events.push(structuredClone(state.current.dropped_labels[j])); changed(); render(); }}, 'It is an event'),
      )));
  }

  function notEventsSection() {
    if (!labels.not_events.length) return '';
    return section('Not events', 'parsing any of these is an error', [h('div', {class: 'chips'},
      labels.not_events.map((t, k) => h('span', {class: 'chip'}, t,
        h('button', {title: 'Remove', onclick: () => { labels.not_events.splice(k, 1); changed(); render(); }}, '×'))))]);
  }

  function failuresSection() {
    const failures = state.comparison.failures;
    if (!failures.length) return '';
    const reasons = Object.fromEntries(labels.known_failures.map(k => [k.path, k.reason || '']));
    return section('Known failures', 'saved with the case so tests pass until the scraper is fixed', failures.map(path =>
      h('div', {class: 'row-item'},
        h('code', {}, path),
        state.comparison.new_failures.includes(path) ? h('span', {class: 'badge'}, 'new') : null,
        (() => {
          const input = h('input', {class: 'reason', placeholder: 'why the scraper gets this wrong', value: reasons[path] || ''});
          input.addEventListener('input', () => {
            const entry = labels.known_failures.find(k => k.path === path);
            if (entry) entry.reason = input.value; else labels.known_failures.push({path, reason: input.value});
            changed();
          });
          return input;
        })(),
      )));
  }

  function settingsSection() {
    const ctx = h('textarea', {rows: 4, class: 'code', value: JSON.stringify(labels.seed_context, null, 1)});
    ctx.addEventListener('change', () => {
      try { labels.seed_context = JSON.parse(ctx.value || '{}'); ctx.classList.remove('invalid'); changed(); render(); }
      catch { ctx.classList.add('invalid'); }
    });
    const notes = h('textarea', {rows: 2, value: labels.notes});
    notes.addEventListener('input', () => { labels.notes = notes.value; changed(); });
    const complete = h('input', {type: 'checkbox'});
    complete.checked = labels.complete;
    complete.addEventListener('change', () => { labels.complete = complete.checked; changed(); render(); });
    const kind = h('select', {}, h('option', {value: 'listing'}, 'listing'), h('option', {value: 'detail'}, 'detail'));
    kind.value = labels.kind;
    kind.addEventListener('change', () => { labels.kind = kind.value; changed(); });
    const c = state.case;
    return section('Case', '', [h('div', {class: 'settings'},
      h('label', {class: 'check'}, complete, ' Labels cover every event on the page'),
      h('label', {}, 'Kind ', kind),
      h('label', {}, 'Notes', notes),
      h('label', {}, 'Seed context (venue, timezone)', ctx),
      h('p', {class: 'sub'}, `source ${c.source}${c.source_ref ? ` (${c.source_ref})` : ''} · captured ${c.captured_at}`,
        c.reviewed_at ? ` · reviewed ${c.reviewed_at.slice(0, 10)} by ${c.reviewer || '?'}` : '',
        c.has_ai_response ? ' · AI answer recorded' : ''),
    )]);
  }

  // ── Left pane ───────────────────────────────────────────────────────────────
  function setupLeft() {
    document.querySelectorAll('#left-tabs button').forEach(btn => btn.addEventListener('click', () => {
      document.querySelectorAll('#left-tabs button').forEach(b => b.classList.toggle('on', b === btn));
      document.querySelectorAll('.tab-body').forEach(b => { b.hidden = b.dataset.tab !== btn.dataset.tab; });
    }));
    document.getElementById('raw-html').textContent = R.rawHtml;
    call('GET', `${api}/sources`).then(src => {
      document.getElementById('page-text').textContent = src.text || '(trafilatura found no text)';
      const box = document.getElementById('structured');
      const block = (title, value) => h('div', {class: 'block'}, h('h3', {}, title),
        h('pre', {class: 'text code'}, value && (Array.isArray(value) ? value.length : Object.keys(value).length) ? JSON.stringify(value, null, 2) : 'none'));
      box.replaceChildren(block('JSON-LD / microdata events', src.jsonld), block('Inline JSON events', src.inline_json), block('OpenGraph', src.opengraph));
    });
    document.addEventListener('selectionchange', () => {
      const sel = document.getSelection();
      const inLeft = sel && sel.anchorNode && document.querySelector('.pane.left').contains(sel.anchorNode);
      if (inLeft) selection = sel.toString().trim();
      document.body.classList.toggle('has-selection', !!selection && inLeft);
    });
  }

  // ── Wire up ─────────────────────────────────────────────────────────────────
  document.getElementById('save').addEventListener('click', save);
  document.getElementById('rerun').addEventListener('click', () => rerun(false));
  document.getElementById('rerun-live')?.addEventListener('click', () => rerun(true));
  document.getElementById('promote')?.addEventListener('click', async (e) => {
    if (!labels.events.length && !confirm('No events are labelled. Save as a fixture asserting the page has none?')) return;
    await busy(e.target, 'Saving…', async () => {
      const {url} = await call('POST', `${api}/promote`, payload());
      setDirty(false); location.href = url;
    });
  });
  document.getElementById('discard')?.addEventListener('click', async () => {
    if (!confirm('Delete this case from the inbox?')) return;
    const {url} = await call('POST', `${api}/discard`);
    setDirty(false); location.href = url;
  });
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); if (dirty) save(); }
  });
  window.addEventListener('beforeunload', e => { if (dirty) e.preventDefault(); });

  setupLeft();
  call('GET', api).then(data => adopt(data)).catch(e => {
    document.getElementById('editor').replaceChildren(h('p', {class: 'error'}, e.message));
  });
})();
