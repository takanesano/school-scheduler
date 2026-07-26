"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const OBJ_LABELS = {
  student_double_day: "One lesson per day per student",
  student_day_gap: "Multiple lessons on a day must be consecutive",
  student_teacher_pair: "Students taught by their assigned teacher",
  teacher_slot_spread: "Even lesson counts across teachers",
  teacher_working_day: "Few teacher working days",
  teacher_single_day: "Few teacher days with too few lessons",
  teacher_day_spread: "Even working-day counts across teachers",
};

const state = { tab: "schedule", keep: false, caution: true,
                compress: true, exact: false, exactBudget: 8,
                lastGen: null,
                selectMode: false, selectedLessons: new Set(),
                repeatWeeks: 4, gridClip: null,
                addSel: null, reopenSlotAdd: null,
                objOrder: Object.keys(OBJ_LABELS),
                hiddenTeachers: new Set(), hiddenStudents: new Set(),
                filterSort: "name",
                calView: "overview", calPerson: null };

const TABS = [
  ["schedule", "Schedule"],
  ["calendars", "Calendars"],
  ["students", "Students"],
  ["teachers", "Teachers"],
  ["subjects", "Subjects"],
  ["rooms", "Rooms"],
  ["timeslots", "Timeslots"],
  ["needs", "Student needs"],
  ["assignments", "Assignments"],
  ["availability", "Availability"],
  ["csv", "CSV import/export"],
];

// ------------------------------------------------------------------ helpers

// A stable, distinct color per teacher, derived from the id: the hash
// is spread by the golden angle so even sequential ids (t01, t02, …)
// land far apart on the hue wheel.
function teacherColor(id) {
  let h = 0;
  for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${Math.round((h * 137.508) % 360)}, 62%, 42%)`;
}

// Big people × slots grids: scroll inside the box with STICKY headers
// (top row(s) and the name column stay visible), plus a hover
// cross-highlight so a cell is easy to trace to its row and column.
// Drag across data cells to select a rectangle (a plain click still
// performs the cell's own action). onSel(rect) fires when a drag
// finishes; rect is {r1,c1,r2,c2} in tbody-row / data-column
// coordinates (the row-header column is excluded).
function attachAreaSelect(table, onSel) {
  let mode = null;        // "cells" | "row" | "col"
  let anchor = null;      // cells: {r,c}; row: r; col: {c, span}
  let last = null;
  let dragging = false;
  let suppress = false;
  let downXY = null;
  let mouseXY = null;
  let scrollTimer = null;
  let moveTracker = null;
  const wrap = table.closest(".grid-scroll");

  const dims = () => {
    const rows = table.tBodies[0].rows;
    return { nr: rows.length,
             nc: rows.length ? rows[0].cells.length - 1 : 0 };
  };
  const clear = () => {
    for (const c of table.querySelectorAll(".area-sel")) {
      c.classList.remove("area-sel");
    }
  };
  const cellPos = (t) => {
    const td = t.closest ? t.closest("td") : null;
    if (!td || td.cellIndex === 0 || !td.closest("tbody")) return null;
    return { r: td.parentElement.sectionRowIndex, c: td.cellIndex - 1 };
  };
  // row headers (tbody th) select whole rows; column headers (thead
  // th, colspan-aware — a date header selects all its periods) select
  // whole columns
  const headerPos = (t) => {
    const th = t.closest ? t.closest("th") : null;
    if (!th || !th.closest("table") || th.closest("table") !== table) {
      return null;
    }
    if (th.closest("tbody")) {
      return { type: "row", r: th.parentElement.sectionRowIndex };
    }
    if (th.closest("thead") && th.cellIndex > 0) {
      let c = 0;
      for (const sib of th.parentElement.cells) {
        if (sib === th) break;
        c += sib.colSpan;
      }
      return { type: "col", c: c - 1, span: th.colSpan };
    }
    return null;
  };
  const rect = () => {
    const { nr, nc } = dims();
    if (mode === "row") {
      return { r1: Math.min(anchor, last), r2: Math.max(anchor, last),
               c1: 0, c2: nc - 1 };
    }
    if (mode === "col") {
      return { r1: 0, r2: nr - 1,
               c1: Math.min(anchor.c, last.c),
               c2: Math.max(anchor.c + anchor.span - 1,
                            last.c + last.span - 1) };
    }
    return { r1: Math.min(anchor.r, last.r),
             r2: Math.max(anchor.r, last.r),
             c1: Math.min(anchor.c, last.c),
             c2: Math.max(anchor.c, last.c) };
  };
  const paint = () => {
    clear();
    const rc = rect();
    const rows = table.tBodies[0].rows;
    for (let r = rc.r1; r <= rc.r2; r++) {
      for (let c = rc.c1; c <= rc.c2; c++) {
        const cell = rows[r] && rows[r].cells[c + 1];
        if (cell) cell.classList.add("area-sel");
      }
    }
    return rc;
  };
  const track = (t) => {   // update `last` from an event target
    if (mode === "cells") {
      const p = cellPos(t);
      if (p) { last = p; return true; }
    } else if (mode === "row") {
      const h = headerPos(t);
      if (h && h.type === "row") { last = h.r; return true; }
      const p = cellPos(t);
      if (p) { last = p.r; return true; }
    } else if (mode === "col") {
      const h = headerPos(t);
      if (h && h.type === "col") { last = h; return true; }
      const p = cellPos(t);
      if (p) { last = { c: p.c, span: 1 }; return true; }
    }
    return false;
  };
  // scroll the grid box when the cursor sits near (or past) its edge
  // mid-drag, and keep extending the selection to the cell that ends
  // up under the cursor
  const autoScroll = () => {
    if (!dragging || !wrap || !mouseXY) return;
    const r = wrap.getBoundingClientRect();
    const M = 30, S = 26;
    let dx = 0, dy = 0;
    if (mouseXY[0] < r.left + M) dx = -S;
    else if (mouseXY[0] > r.right - M) dx = S;
    if (mouseXY[1] < r.top + M) dy = -S;
    else if (mouseXY[1] > r.bottom - M) dy = S;
    if (!dx && !dy) return;
    wrap.scrollLeft += dx;
    wrap.scrollTop += dy;
    const headH = table.tHead ? table.tHead.offsetHeight : 0;
    const x = Math.min(Math.max(mouseXY[0], r.left + 4), r.right - 4);
    const y = Math.min(Math.max(mouseXY[1], r.top + headH + 4),
                       r.bottom - 4);
    const under = document.elementFromPoint(x, y);
    if (under && track(under)) paint();
  };
  const finish = () => {
    if (moveTracker) document.removeEventListener("mousemove", moveTracker);
    moveTracker = null;
    clearInterval(scrollTimer);
    if (dragging) {
      onSel(paint());
      suppress = true;   // swallow the click that follows the drag
    }
    mode = null;
    anchor = null;
    dragging = false;
  };
  table.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    suppress = false;
    const h = headerPos(e.target);
    const p = cellPos(e.target);
    if (h && h.type === "row") {
      mode = "row"; anchor = h.r; last = h.r; dragging = true; paint();
    } else if (h && h.type === "col") {
      mode = "col"; anchor = h; last = h; dragging = true; paint();
    } else if (p) {
      mode = "cells"; anchor = p; last = p; dragging = false;
    } else return;
    downXY = [e.clientX, e.clientY];
    mouseXY = downXY;
    e.preventDefault();          // no text selection while dragging
    moveTracker = (ev) => { mouseXY = [ev.clientX, ev.clientY]; };
    document.addEventListener("mousemove", moveTracker);
    scrollTimer = setInterval(autoScroll, 40);
    document.addEventListener("mouseup", finish, { once: true });
  });
  table.addEventListener("mouseover", (e) => {
    if (!mode) return;
    if (!track(e.target)) return;
    if (mode === "cells" && !dragging) {
      if (last.r !== anchor.r || last.c !== anchor.c) dragging = true;
      else return;
    }
    paint();
  });
  // a small wiggle INSIDE one cell also starts a selection, so a
  // single cell can be selected (e.g. as a paste anchor); a clean
  // click still runs the cell's own action
  table.addEventListener("mousemove", (e) => {
    if (mode !== "cells" || dragging || !downXY) return;
    if (Math.abs(e.clientX - downXY[0])
        + Math.abs(e.clientY - downXY[1]) > 5) {
      dragging = true;
      paint();
    }
  });
  table.addEventListener("click", (e) => {
    if (suppress) {
      e.stopPropagation();
      e.preventDefault();
      suppress = false;
    }
  }, true);
  return { clear };
}

let _gridSeq = 0;
function enhanceGrid(wrap) {
  const table = $("table", wrap);
  if (!table) return;
  wrap.classList.add("grid-scroll");
  const id = table.id || (table.id = `grid-${++_gridSeq}`);
  // two-row headers: the second row sticks below the first one
  requestAnimationFrame(() => {
    const rows = table.tHead ? table.tHead.rows : [];
    if (rows.length > 1 && rows[0].offsetHeight) {
      table.style.setProperty("--head1-h", rows[0].offsetHeight + "px");
    }
  });
  // column highlight: swap ONE stylesheet rule instead of touching
  // hundreds of cells per mousemove
  const style = document.createElement("style");
  wrap.append(style);
  let last = 0;
  table.addEventListener("mouseover", (e) => {
    const cell = e.target.closest("td, th");
    if (!cell || cell.closest("table") !== table) return;
    const idx = cell.cellIndex + 1;
    if (idx === last) return;
    last = idx;
    style.textContent = idx <= 1 ? "" :
      `#${id} tbody td:nth-child(${idx}),
       #${id} thead tr:last-child th:nth-child(${idx}) {
         box-shadow: inset 0 0 0 999px rgba(47, 111, 237, 0.12); }`;
  });
  table.addEventListener("mouseleave", () => {
    style.textContent = "";
    last = 0;
  });
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, isError ? 8000 : 3000);
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const d = data && data.detail;
    let msg = typeof d === "string" ? d : "";
    if (d && d.errors) msg = d.errors.join("\n");
    if (d && d.violations) msg = d.violations.map(v => v.message).join("\n");
    if (!msg) msg = `${method} ${path} failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

const list = (entity) => api("GET", `/api/${entity}`);

// In-page replacement for window.confirm(). Native confirm dialogs carry a
// browser-level "prevent additional dialogs" checkbox that, once ticked,
// silently auto-cancels every later dialog and breaks the caution flow —
// so we never use them.
function appConfirm(message, okLabel = "OK") {
  return new Promise((resolve) => {
    const overlay = el(`<div class="modal-overlay">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-msg">${esc(message)}</div>
        <div class="modal-actions">
          <button class="action" id="m-ok">${esc(okLabel)}</button>
          <button class="action secondary" id="m-cancel">Cancel</button>
        </div></div></div>`);
    const done = (v) => {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
      if (e.key === "Enter") done(true);
    };
    document.addEventListener("keydown", onKey);
    $("#m-ok", overlay).onclick = () => done(true);
    $("#m-cancel", overlay).onclick = () => done(false);
    overlay.onclick = (e) => { if (e.target === overlay) done(false); };
    document.body.append(overlay);
    $("#m-cancel", overlay).focus();
  });
}

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function sortSlots(slots) {
  return [...slots].sort((a, b) =>
    a.date < b.date ? -1 : a.date > b.date ? 1 : a.period - b.period);
}
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function fmtDate(iso) {  // "2026-07-27" -> "7/27"
  return `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
}
function weekdayOf(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return WEEKDAYS[(new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7];
}
const slotLabel = (s) =>
  `${fmtDate(s.date)} ${weekdayOf(s.date)} P${s.period}` +
  (s.label ? ` (${s.label})` : "");

// -------------------------------------------------------------- basic tables

async function renderNamedTable(root, entity, title) {
  const rows = await list(entity);
  const panel = el(`<div class="panel"><h2>${title}</h2>
    <div class="row">
      <input id="new-id" placeholder="id (e.g. ${entity.slice(0, 3)}1)">
      <input id="new-name" placeholder="name">
      <button class="action" id="add">Add / update</button>
    </div>
    <table><thead><tr><th>ID</th><th>Name</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of rows) {
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td>
      <td><button class="small">delete</button></td></tr>`);
    $("button", tr).onclick = async () => {
      if (!await appConfirm(`Delete ${r.id}? Related availability, needs and lessons are removed too.`, "Delete")) return;
      await api("DELETE", `/api/${entity}/${encodeURIComponent(r.id)}`).catch(e => toast(e.message, true));
      render();
    };
    tbody.append(tr);
  }
  $("#add", panel).onclick = async () => {
    const id = $("#new-id", panel).value.trim();
    const name = $("#new-name", panel).value.trim();
    if (!id || !name) return toast("id and name are required", true);
    try { await api("POST", `/api/${entity}`, { id, name }); render(); }
    catch (e) { toast(e.message, true); }
  };
  root.append(panel);
}

async function renderTeachers(root) {
  const [teachers, subjects, tsubs] = await Promise.all([
    list("teachers"), list("subjects"), list("teacher_subjects")]);
  const subname = Object.fromEntries(subjects.map(s => [s.id, s.name]));
  const subjectsOf = {};
  for (const t of tsubs) (subjectsOf[t.teacher_id] ??= []).push(t.subject_id);

  const panel = el(`<div class="panel"><h2>Teachers</h2>
    <div class="row">
      <input id="new-id" placeholder="id (e.g. t1)">
      <input id="new-name" placeholder="name">
      <button class="action" id="add">Add / update</button>
    </div>
    <p class="muted">Max lessons/day = the most lessons that teacher takes
      on one calendar day (0 = no limit).</p>
    <table><thead><tr><th>ID</th><th>Name</th><th>Can teach</th>
      <th>Max lessons/day</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of teachers) {
    const taught = (subjectsOf[r.id] || [])
      .map(id => subname[id] || id).sort().join(", ");
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td>
      <td>${taught ? esc(taught) : '<span class="muted">— none yet —</span>'}</td>
      <td><input type="number" min="0" max="99" class="inline-num"
        value="${r.max_lessons_per_day}" data-daymax
        title="0 = no limit"></td>
      <td><button class="small">delete</button></td></tr>`);
    $("input[data-daymax]", tr).onchange = async (e) => {
      const v = parseInt(e.target.value, 10);
      if (!(v >= 0 && v <= 99)) {
        return toast("max lessons/day must be 0-99", true);
      }
      try {
        await api("POST", "/api/teachers",
          { id: r.id, name: r.name, max_lessons_per_day: v });
        render();   // schedule revalidates against the new limit
      } catch (e2) { toast(e2.message, true); render(); }
    };
    $("button", tr).onclick = async () => {
      if (!await appConfirm(`Delete ${r.id}? Related availability, subjects and lessons are removed too.`, "Delete")) return;
      await api("DELETE", `/api/teachers/${encodeURIComponent(r.id)}`).catch(e => toast(e.message, true));
      render();
    };
    tbody.append(tr);
  }
  $("#add", panel).onclick = async () => {
    const id = $("#new-id", panel).value.trim();
    const name = $("#new-name", panel).value.trim();
    if (!id || !name) return toast("id and name are required", true);
    try { await api("POST", "/api/teachers", { id, name }); render(); }
    catch (e) { toast(e.message, true); }
  };
  root.append(panel);

  // teacher × subject toggle matrix
  if (teachers.length && subjects.length) {
    const have = new Set(tsubs.map(t => `${t.teacher_id}|${t.subject_id}`));
    const grid = el(`<div class="panel"><h2>Who can teach what</h2>
      <p class="muted">Click a cell to toggle. ✓ = the teacher can teach
        that subject.</p>
      <div class="grid-scroll"><table class="grid-table"><thead><tr>
        <th></th>${subjects.map(s =>
          `<th>${esc(s.name)}</th>`).join("")}</tr></thead>
      <tbody></tbody></table></div></div>`);
    const gbody = $("tbody", grid);
    for (const t of teachers) {
      const tr = document.createElement("tr");
      tr.append(el(`<th>${esc(t.name)} (${esc(t.id)})</th>`));
      for (const s of subjects) {
        const key = `${t.id}|${s.id}`;
        const td = el(`<td class="${have.has(key) ? "avail" : "unavail"}">${
          have.has(key) ? "✓" : "·"}</td>`);
        td.onclick = async () => {
          try {
            if (have.has(key)) {
              await api("DELETE",
                `/api/teacher_subjects?teacher_id=${encodeURIComponent(t.id)}&subject_id=${encodeURIComponent(s.id)}`);
              have.delete(key);
            } else {
              await api("POST", "/api/teacher_subjects",
                { teacher_id: t.id, subject_id: s.id });
              have.add(key);
            }
            render();   // keep the "Can teach" column above in sync
          } catch (e) { toast(e.message, true); }
        };
        tr.append(td);
      }
      gbody.append(tr);
    }
    enhanceGrid($(".grid-scroll", grid));
    root.append(grid);
  } else if (teachers.length) {
    root.append(el(`<div class="panel"><p class="muted">
      Add subjects (Subjects tab) to assign what each teacher can teach.</p></div>`));
  }
}

async function renderRooms(root) {
  const rows = await list("rooms");
  const panel = el(`<div class="panel"><h2>Rooms</h2>
    <div class="row">
      <input id="new-id" placeholder="id (e.g. r1)">
      <input id="new-name" placeholder="name">
      <input id="new-cap" type="number" min="1" value="1" style="width:6rem" title="capacity">
      <input id="new-tcap" type="number" min="0" value="0" style="width:6rem"
        title="teacher limit (0 = no limit)">
      <button class="action" id="add">Add / update</button>
    </div>
    <p class="muted">Capacity = how many simultaneous lessons fit in the room
      (e.g. booths in one open room). Teacher limit = how many different
      teachers may be in the room at the same time (0 = no limit).</p>
    <table><thead><tr><th>ID</th><th>Name</th><th>Capacity</th>
      <th>Teacher limit</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of rows) {
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td>
      <td>${r.capacity}</td>
      <td>${r.teacher_capacity || "—"}</td>
      <td><button class="small">delete</button></td></tr>`);
    $("button", tr).onclick = async () => {
      if (!await appConfirm(`Delete room ${r.id}?`, "Delete")) return;
      await api("DELETE", `/api/rooms/${encodeURIComponent(r.id)}`).catch(e => toast(e.message, true));
      render();
    };
    tbody.append(tr);
  }
  $("#add", panel).onclick = async () => {
    const id = $("#new-id", panel).value.trim();
    const name = $("#new-name", panel).value.trim();
    const capacity = parseInt($("#new-cap", panel).value, 10);
    const teacher_capacity = parseInt($("#new-tcap", panel).value, 10);
    if (!id || !name || !(capacity >= 1)) return toast("id, name and capacity ≥ 1 required", true);
    if (!(teacher_capacity >= 0)) return toast("teacher limit must be 0 or more", true);
    try {
      await api("POST", "/api/rooms", { id, name, capacity, teacher_capacity });
      render();
    } catch (e) { toast(e.message, true); }
  };
  root.append(panel);
}

function renderTimeslotsBulk(root) {
  const panel = el(`<div class="panel"><h2>Mass-add timeslots</h2>
    <div class="row">
      from <input id="b-start" type="date">
      to <input id="b-end" type="date">
    </div>
    <div class="row" id="b-days">
      ${WEEKDAYS.map(d => `<label><input type="checkbox" value="${d}"${
        d === "Sun" ? "" : " checked"}> ${d}</label>`).join("")}
    </div>
    <div class="row">
      periods <input id="b-count" type="number" min="1" max="10" value="3"
        style="width:5rem">
      labels <input id="b-labels" style="min-width:22rem"
        placeholder="comma-separated, e.g. 09:00-10:10, 10:20-11:30, 13:00-14:10">
      <button class="action" id="b-add">Add all</button>
    </div>
    <p class="muted">Creates one timeslot per selected weekday and period
      across the date range. Dates that already have a slot for that period
      are skipped, never overwritten.</p></div>`);
  $("#b-add", panel).onclick = async () => {
    const start = $("#b-start", panel).value;
    const end = $("#b-end", panel).value;
    const count = parseInt($("#b-count", panel).value, 10);
    if (!start || !end || !(count >= 1)) {
      return toast("start date, end date and periods ≥ 1 required", true);
    }
    const weekdays = [...panel.querySelectorAll("#b-days input:checked")]
      .map(cb => cb.value);
    const labels = $("#b-labels", panel).value.split(",").map(s => s.trim());
    const periods = Array.from({ length: count }, (_, i) =>
      ({ period: i + 1, label: labels[i] || "" }));
    try {
      const res = await api("POST", "/api/timeslots/bulk",
        { start_date: start, end_date: end, weekdays, periods });
      toast(`Created ${res.created} timeslot(s)` +
        (res.skipped ? `, skipped ${res.skipped} existing` : ""));
      render();
    } catch (e) { toast(e.message, true); }
  };
  root.append(panel);
}

async function renderTimeslots(root) {
  renderTimeslotsBulk(root);
  const rows = sortSlots(await list("timeslots"));
  const panel = el(`<div class="panel"><h2>Timeslots</h2>
    <div class="row">
      <input id="new-id" placeholder="id (e.g. 0727-1)">
      <input id="new-date" type="date">
      <input id="new-period" type="number" min="1" value="1" style="width:6rem" title="period">
      <input id="new-label" placeholder="label e.g. 17:00-18:10 (optional)">
      <button class="action" id="add">Add / update</button>
    </div>
    <p class="muted">Each timeslot is one period on one concrete calendar
      date — every day of the term is unique.</p>
    <table><thead><tr><th>ID</th><th>Date</th><th></th><th>Period</th>
      <th>Label</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of rows) {
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${r.date}</td>
      <td>${weekdayOf(r.date)}</td><td>${r.period}</td>
      <td>${esc(r.label)}</td><td><button class="small">delete</button></td></tr>`);
    $("button", tr).onclick = async () => {
      if (!await appConfirm(`Delete timeslot ${r.id}?`, "Delete")) return;
      await api("DELETE", `/api/timeslots/${encodeURIComponent(r.id)}`).catch(e => toast(e.message, true));
      render();
    };
    tbody.append(tr);
  }
  $("#add", panel).onclick = async () => {
    const body = {
      id: $("#new-id", panel).value.trim(),
      date: $("#new-date", panel).value,
      period: parseInt($("#new-period", panel).value, 10),
      label: $("#new-label", panel).value.trim(),
    };
    if (!body.id || !body.date || !(body.period >= 1)) {
      return toast("id, date and period ≥ 1 required", true);
    }
    try { await api("POST", "/api/timeslots", body); render(); }
    catch (e) { toast(e.message, true); }
  };
  root.append(panel);
}

// --------------------------------------------------------------------- needs

async function renderNeeds(root) {
  const [students, subjects, teachers, needs, tsubs] = await Promise.all([
    list("students"), list("subjects"), list("teachers"),
    list("student_needs"), list("teacher_subjects")]);
  const sname = Object.fromEntries(students.map(s => [s.id, s.name]));
  const subname = Object.fromEntries(subjects.map(s => [s.id, s.name]));
  const tname = Object.fromEntries(teachers.map(t => [t.id, t.name]));

  const needsPanel = el(`<div class="panel"><h2>Student needs (total sessions over the term)</h2>
    <div class="row">
      <select id="n-student">${students.map(s =>
        `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`).join("")}</select>
      <select id="n-subject">${subjects.map(s =>
        `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`).join("")}</select>
      <input id="n-count" type="number" min="1" value="1" style="width:6rem">
      <button class="action" id="add">Set need</button>
    </div>
    <table><thead><tr><th>Student</th><th>Subject</th><th>Sessions</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", needsPanel);
  for (const n of needs) {
    const tr = el(`<tr><td>${esc(sname[n.student_id] || n.student_id)}</td>
      <td>${esc(subname[n.subject_id] || n.subject_id)}</td>
      <td>${n.sessions}</td><td><button class="small">delete</button></td></tr>`);
    $("button", tr).onclick = async () => {
      await api("DELETE",
        `/api/student_needs?student_id=${encodeURIComponent(n.student_id)}&subject_id=${encodeURIComponent(n.subject_id)}`)
        .catch(e => toast(e.message, true));
      render();
    };
    tbody.append(tr);
  }
  $("#add", needsPanel).onclick = async () => {
    try {
      await api("POST", "/api/student_needs", {
        student_id: $("#n-student", needsPanel).value,
        subject_id: $("#n-subject", needsPanel).value,
        sessions: parseInt($("#n-count", needsPanel).value, 10),
      });
      render();
    } catch (e) { toast(e.message, true); }
  };
  root.append(needsPanel);

  const tsPanel = el(`<div class="panel"><h2>Teacher subjects (who can teach what)</h2>
    <div class="row">
      <select id="t-teacher">${teachers.map(t =>
        `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.id)})</option>`).join("")}</select>
      <select id="t-subject">${subjects.map(s =>
        `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`).join("")}</select>
      <button class="action" id="add">Add</button>
    </div>
    <table><thead><tr><th>Teacher</th><th>Subject</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tsBody = $("tbody", tsPanel);
  for (const t of tsubs) {
    const tr = el(`<tr><td>${esc(tname[t.teacher_id] || t.teacher_id)}</td>
      <td>${esc(subname[t.subject_id] || t.subject_id)}</td>
      <td><button class="small">delete</button></td></tr>`);
    $("button", tr).onclick = async () => {
      await api("DELETE",
        `/api/teacher_subjects?teacher_id=${encodeURIComponent(t.teacher_id)}&subject_id=${encodeURIComponent(t.subject_id)}`)
        .catch(e => toast(e.message, true));
      render();
    };
    tsBody.append(tr);
  }
  $("#add", tsPanel).onclick = async () => {
    try {
      await api("POST", "/api/teacher_subjects", {
        teacher_id: $("#t-teacher", tsPanel).value,
        subject_id: $("#t-subject", tsPanel).value,
      });
      render();
    } catch (e) { toast(e.message, true); }
  };
  root.append(tsPanel);
}

// -------------------------------------------------------------- availability

async function renderAvailability(root) {
  const [students, teachers, slots] = await Promise.all([
    list("students"), list("teachers"), list("timeslots")]);
  const sorted = sortSlots(slots);
  if (!sorted.length) {
    root.append(el(`<div class="panel"><p class="muted">
      Define timeslots first (Timeslots tab).</p></div>`));
    return;
  }
  const undoRow = el(`<div class="row"></div>`);
  undoRow.append(await gridUndoButton());
  root.append(undoRow);
  await renderAvailGrid(root, "Teacher availability", teachers,
    "teacher_availability", "teacher_id", sorted);
  await renderAvailGrid(root, "Student availability", students,
    "student_availability", "student_id", sorted);
}

async function renderAvailGrid(root, title, people, entity, idCol, slots) {
  const links = await list(entity);
  const have = new Set(links.map(r => `${r[idCol]}|${r.timeslot_id}`));
  // two-row header: dates spanning their periods, then one column per slot
  const dates = [];
  for (const s of slots) {
    const last = dates[dates.length - 1];
    if (last && last.date === s.date) last.slots.push(s);
    else dates.push({ date: s.date, slots: [s] });
  }
  const panel = el(`<div class="panel"><h2>${title}</h2>
    <p class="muted">Click a cell to toggle. ✓ = available.</p>
    <div class="grid-scroll"><table class="grid-table"><thead>
      <tr><th></th>${dates.map(d =>
        `<th colspan="${d.slots.length}">${fmtDate(d.date)}<br>
         <span class="muted">${weekdayOf(d.date)}</span></th>`).join("")}</tr>
      <tr><th></th>${slots.map(s => `<th>P${s.period}</th>`).join("")}</tr>
    </thead><tbody></tbody></table></div></div>`);
  const tbody = $("tbody", panel);
  for (const p of people) {
    const tr = document.createElement("tr");
    tr.append(el(`<th>${esc(p.name)} (${esc(p.id)})</th>`));
    for (const s of slots) {
      const on = have.has(`${p.id}|${s.id}`);
      const td = el(`<td class="${on ? "avail" : "unavail"}">${on ? "✓" : "·"}</td>`);
      td.onclick = async () => {
        try {
          if (have.has(`${p.id}|${s.id}`)) {
            await api("DELETE",
              `/api/${entity}?${idCol}=${encodeURIComponent(p.id)}&timeslot_id=${encodeURIComponent(s.id)}`);
            have.delete(`${p.id}|${s.id}`);
            td.className = "unavail"; td.textContent = "·";
          } else {
            await api("POST", `/api/${entity}`, { [idCol]: p.id, timeslot_id: s.id });
            have.add(`${p.id}|${s.id}`);
            td.className = "avail"; td.textContent = "✓";
          }
        } catch (e) { toast(e.message, true); }
      };
      tr.append(td);
    }
    tbody.append(tr);
  }
  enhanceGrid($(".grid-scroll", panel));

  // drag-select a rectangle -> bulk actions on the whole block
  const bar = el(`<div class="row area-bar" hidden>
    <span class="muted area-count"></span>
    <button class="action secondary" data-act="on">✓ available</button>
    <button class="action secondary" data-act="off">· unavailable</button>
    <button class="action secondary" data-act="inv">invert</button>
    <button class="action secondary" data-act="copy">copy</button>
    <button class="action secondary" data-act="paste">paste</button>
    <button class="action secondary" data-act="unsel">deselect</button>
  </div>`);
  panel.insertBefore(bar, $(".grid-scroll", panel));
  let rect = null;
  const selApi = attachAreaSelect($("table", panel), (r) => {
    rect = r;
    bar.hidden = false;
    const n = (r.r2 - r.r1 + 1) * (r.c2 - r.c1 + 1);
    $(".area-count", bar).textContent = `${n} cells`;
    $('[data-act="paste"]', bar).disabled =
      !(state.gridClip && state.gridClip.kind === "avail");
  });
  bar.onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b || !rect) return;
    const act = b.dataset.act;
    if (act === "unsel") {
      selApi.clear();
      rect = null;
      bar.hidden = true;
      return;
    }
    if (act === "copy") {
      const vals = [];
      for (let r = rect.r1; r <= rect.r2; r++) {
        const row = [];
        for (let c = rect.c1; c <= rect.c2; c++) {
          row.push(have.has(`${people[r].id}|${slots[c].id}`));
        }
        vals.push(row);
      }
      state.gridClip = { kind: "avail", vals };
      toast(`Copied ${vals.length} × ${vals[0].length} cells`);
      $('[data-act="paste"]', bar).disabled = false;
      return;
    }
    const body = { add: [], remove: [] };
    if (act === "paste") {
      const clip = state.gridClip;
      if (!clip || clip.kind !== "avail") return;
      for (let i = 0; i < clip.vals.length; i++) {
        for (let j = 0; j < clip.vals[i].length; j++) {
          const r = rect.r1 + i, c = rect.c1 + j;
          if (r >= people.length || c >= slots.length) continue;
          (clip.vals[i][j] ? body.add : body.remove)
            .push([people[r].id, slots[c].id]);
        }
      }
    } else {
      for (let r = rect.r1; r <= rect.r2; r++) {
        for (let c = rect.c1; c <= rect.c2; c++) {
          const pair = [people[r].id, slots[c].id];
          const on = have.has(`${people[r].id}|${slots[c].id}`);
          if (act === "on") body.add.push(pair);
          else if (act === "off") body.remove.push(pair);
          else (on ? body.remove : body.add).push(pair);
        }
      }
    }
    try {
      const res = await api("POST", `/api/${entity}/bulk`, body);
      toast(`Updated: ${res.added} available, ${res.removed} cleared`);
      render();
    } catch (e2) { toast(e2.message, true); }
  };
  root.append(panel);
}

// ---------------------------------------------------------------------- CSV

const CSV_ENTITIES = [
  ["students", "id,name"],
  ["teachers", "id,name,max_lessons_per_day"],
  ["subjects", "id,name"],
  ["rooms", "id,name,capacity,teacher_capacity"],
  ["timeslots", "id,date,period,label"],
  ["teacher_subjects", "teacher_id,subject_id"],
  ["teacher_students", "teacher_id,student_id,priority"],
  ["student_needs", "student_id,subject_id,sessions"],
  ["teacher_availability", "teacher_id,timeslot_id"],
  ["student_availability", "student_id,timeslot_id"],
];

function renderCsv(root) {
  const panel = el(`<div class="panel"><h2>CSV import / export</h2>
    <p class="muted">Import replaces that table's contents with the file
      (all-or-nothing; invalid files change nothing). Import base tables
      (students, teachers, subjects, rooms, timeslots) before link tables.
      Dates are YYYY-MM-DD.</p>
    <table><thead><tr><th>Entity</th><th>Header</th><th>Import</th><th>Export</th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const [entity, header] of CSV_ENTITIES) {
    const tr = el(`<tr><td>${entity}</td><td><code>${header}</code></td>
      <td><input type="file" accept=".csv,text/csv"></td>
      <td><a href="/api/export/${entity}" download>${entity}.csv</a></td></tr>`);
    $("input", tr).onchange = async (ev) => {
      const file = ev.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await api("POST", `/api/import/${entity}`, fd);
        toast(`Imported ${res.rows} row(s) into ${entity}`);
      } catch (e) { toast(e.message, true); }
      ev.target.value = "";
    };
    tbody.append(tr);
  }
  root.append(panel);

  // whole-database backup: one file carries EVERYTHING (master data,
  // schedule, locks, assignments, settings)
  const backup = el(`<div class="panel"><h2>Database backup</h2>
    <p class="muted">The whole database in one file — master data, the
      schedule with its locks, assignments and settings. Download
      regularly, and before big changes; restoring replaces
      <b>everything</b> with the backup's contents.</p>
    <div class="row">
      <a class="action secondary" href="/api/backup.db">Download backup
        (.db)</a>
      <label class="action secondary" style="cursor:pointer">
        Restore from backup…
        <input type="file" id="restore-db" accept=".db"
          style="display:none"></label>
    </div></div>`);
  $("#restore-db", backup).onchange = async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    if (!await appConfirm(
      `Restore "${file.name}"?\n\nThis REPLACES the entire current `
      + "database — schedule, students, teachers, settings, everything. "
      + "Current data will be lost.\n\nTip: download a backup of the "
      + "current state first.", "Replace everything")) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api("POST", "/api/backup.db", fd);
      state.lastGen = null;
      state.selectedLessons.clear();
      toast(`Restored: ${res.students} students, ${res.teachers} `
        + `teachers, ${res.lessons} lessons`);
      render();
    } catch (e) { toast(e.message, true); }
  };
  root.append(backup);
}

// ---------------------------------------------------- shared calendar table

// data: {periods, weeks}; entryHtml(entry, slotCell) -> element to append.
// slotHook(block, slot), if given, runs per rendered slot block (e.g. to
// make it a drag-and-drop target on the Schedule tab).
function calendarTable(data, entryHtml, slotHook) {
  const wrap = el(`<div style="overflow-x:auto"><table class="cal-table">
    <thead><tr>${WEEKDAYS.map(d => `<th>${d}</th>`).join("")}</tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", wrap);
  for (const week of data.weeks) {
    const tr = document.createElement("tr");
    for (const cell of week) {
      const td = document.createElement("td");
      td.className = cell.in_term ? "cal-day" : "cal-day cal-off";
      td.append(el(`<div class="cal-date">${fmtDate(cell.date)}</div>`));
      for (const slot of cell.slots) {
        const block = el(`<div class="cal-slot">
          <span class="cal-period">P${slot.period}${
            slot.label ? ` <span class="muted">${esc(slot.label)}</span>` : ""
          }</span></div>`);
        for (const entry of slot.entries) block.append(entryHtml(entry, slot));
        if (slotHook) slotHook(block, slot);
        td.append(block);
      }
      tr.append(td);
    }
    tbody.append(tr);
  }
  return wrap;
}

// ------------------------------------------------------------------ schedule

async function renderSchedule(root) {
  const [schedule, check, overview, settings, students, teachers, subjects,
         rooms, slots] =
    await Promise.all([
      api("GET", "/api/schedule"),
      api("GET", "/api/schedule/check"),
      api("GET", "/api/views/overview"),
      api("GET", "/api/settings"),
      list("students"), list("teachers"), list("subjects"),
      list("rooms"), list("timeslots")]);
  const sorted = sortSlots(slots);

  const ctrl = el(`<div class="panel"><h2>Generate</h2>
    <div class="gen-groups">
      <fieldset class="gen-group"><legend>Objectives &amp; constraints</legend>
        <div class="obj-list">
          <div id="hard-zone">
            <div class="obj-zone-title">🔒 Always active
              <span class="muted">(priority 0)</span></div>
            <ul class="locked-rules">
              <li>🔒 teachers teach only their subjects, only when available</li>
              <li>🔒 students only when available; one lesson per timeslot</li>
              <li>🔒 room capacity (and its teacher limit, if set) is
                never exceeded</li>
              <li>🔒 a teacher teaches at most ${settings.teacher_capacity}
                students per timeslot</li>
              <li>🔒 a student has at most ${settings.student_day_cap} lessons
                per day</li>
            </ul>
            <ul id="hard-objs"></ul>
          </div>
          <div class="obj-divider" id="obj-divider">— drag a card above this
            line to make it priority 0 (always active) —</div>
          <ul id="prio-list"></ul>
        </div>
        <label id="compress-label"><input type="checkbox" id="opt-compress"${
          state.compress ? " checked" : ""}${state.exact ? " disabled" : ""}>
          optimize these priorities (standard solver)</label>
        <p class="muted" id="compress-note"${state.exact ? "" : " hidden"}>
          The exact optimizer always optimizes all priorities at once —
          this toggle only applies to the standard solver.</p>
      </fieldset>
      <fieldset class="gen-group"><legend>Solver</legend>
        <label><input type="checkbox" id="opt-keep"${state.keep ? " checked" : ""}>
          keep existing lessons</label>
        <label><input type="radio" name="solver-pick" id="opt-standard"${
          state.exact ? "" : " checked"}>
          <b>standard</b> — fast, good-but-approximate
          <span class="muted">(usually well under a second)</span></label>
        <label title="Models the whole problem as a constraint program
          (OR-tools CP-SAT) and optimizes all priorities at once. Falls
          back to the standard solver automatically when it cannot do
          better.">
          <input type="radio" name="solver-pick" id="opt-exact"${
            state.exact ? " checked" : ""}>
          <b>exact</b> (CP-SAT) — slower, best schedule it can prove
          within its budget</label>
        <div id="exact-opts"${state.exact ? "" : " hidden"}>
          <label class="gen-inline">search budget
            <input type="number" id="opt-exact-budget" min="1" max="600"
              value="${state.exactBudget}" style="width:5rem"> s</label>
          <div class="warning gen-warning">⏳ The exact optimizer keeps
            searching for the whole budget, so generating will take
            roughly this long. Larger budgets can find better
            schedules.</div>
        </div>
      </fieldset>
    </div>
    <div class="row gen-actions">
      <button class="action secondary" id="clear">Clear schedule</button>
      <button class="action" id="gen">Generate schedule</button>
    </div>
    <div id="gen-result"></div></div>`);
  // ONE continuous list of objective cards. Cards above the divider are
  // priority 0 = always active (a hard cap, stored in settings); cards
  // below are the soft priorities 1..n. Dragging across the divider
  // changes which side a card is on.
  const caps = settings.objective_caps || {};
  const CAP_DEFAULTS = { student_double_day: 0, student_day_gap: 0,
                         student_teacher_pair: 0,
                         teacher_slot_spread: 1, teacher_working_day: 30,
                         teacher_single_day: 0, teacher_day_spread: 1 };
  let dragKey = null;

  async function putObjSettings(patch) {
    try {
      await api("PUT", "/api/settings", {
        teacher_capacity: settings.teacher_capacity,
        student_day_cap: settings.student_day_cap,
        single_day_max: settings.single_day_max,
        objective_caps: caps,
        ...patch,
      });
      render();   // everything revalidates against the new rules
    } catch (e) { toast(e.message, true); render(); }
  }
  const putCaps = (newCaps) => putObjSettings({ objective_caps: newCaps });

  // moved card ends up at rank 0 (hard) or a soft position around target
  function settle(moved, { hard, targetKey = null, after = false,
                           atStart = false }) {
    const rest = state.objOrder.filter(k => k !== moved);
    if (targetKey) {
      rest.splice(rest.indexOf(targetKey) + (after ? 1 : 0), 0, moved);
    } else if (hard) {
      rest.unshift(moved);
    } else if (atStart) {
      const idx = rest.findIndex(k => !(k in caps));
      rest.splice(idx === -1 ? rest.length : idx, 0, moved);
    } else {
      rest.push(moved);
    }
    state.objOrder = rest;
    const wasHard = moved in caps;
    if (hard && !wasHard) putCaps({ ...caps, [moved]: CAP_DEFAULTS[moved] });
    else if (!hard && wasHard) {
      const nc = { ...caps };
      delete nc[moved];
      putCaps(nc);
    } else renderObjList();
  }

  // Drop-position preview: a line on the exact edge where the dragged
  // card will be inserted (never a whole-area highlight).
  function clearDropMarks() {
    for (const n of ctrl.querySelectorAll(
      ".drop-before, .drop-after, .drop-line")) {
      n.classList.remove("drop-before", "drop-after", "drop-line");
    }
  }
  function markEdge(node, after) {
    clearDropMarks();
    if (node) node.classList.add(after ? "drop-after" : "drop-before");
  }
  function markListStart(ul) {          // insertion at the top of a list
    const first = ul.querySelector(".prio-item");
    if (first) markEdge(first, false);
    else { clearDropMarks(); ul.classList.add("drop-line"); }
  }
  function markListEnd(ul) {            // insertion at the bottom
    const items = ul.querySelectorAll(".prio-item");
    if (items.length) markEdge(items[items.length - 1], true);
    else { clearDropMarks(); ul.classList.add("drop-line"); }
  }

  function makeCard(key, rank) {
    const hard = rank === 0;
    // the single-day term's threshold is edited right on the card:
    // "few teacher days with at most [N] lesson(s)"
    const label = key === "teacher_single_day"
      ? `Few teacher days with at most <input type="number" min="1"
          max="10" value="${settings.single_day_max}" data-sdm
          class="inline-num"> lesson(s)`
      : esc(OBJ_LABELS[key]);
    const li = el(hard
      ? `<li class="prio-item hard-obj" draggable="true" data-key="${key}">
          <span class="prio-rank rank-zero">0</span> <span>${label}</span>
          <span class="hard-bound">≤ <input type="number" min="0" max="999"
            value="${caps[key]}" data-bound="${key}"></span>
          <span class="prio-grip">⠿</span></li>`
      : `<li class="prio-item" draggable="true" data-key="${key}">
          <span class="prio-rank">${rank}</span> <span>${label}</span>
          <span class="prio-grip">⠿</span></li>`);
    li.ondragstart = (e) => {
      dragKey = key;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", key);
      li.classList.add("dragging");
    };
    li.ondragend = () => {
      li.classList.remove("dragging");
      clearDropMarks();
    };
    li.ondragover = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = li.getBoundingClientRect();
      markEdge(li, e.clientY > rect.top + rect.height / 2);
    };
    li.ondrop = (e) => {
      e.preventDefault();
      e.stopPropagation();
      clearDropMarks();
      const moved = dragKey || e.dataTransfer.getData("text/plain");
      dragKey = null;
      if (!moved || moved === key) return;
      const rect = li.getBoundingClientRect();
      settle(moved, { hard, targetKey: key,
                      after: e.clientY > rect.top + rect.height / 2 });
    };
    if (hard) {
      const bound = li.querySelector("input[data-bound]");
      bound.onchange = () => {
        const v = parseInt(bound.value, 10);
        if (!(v >= 0 && v <= 999)) return toast("bound must be 0-999", true);
        putCaps({ ...caps, [key]: v });
      };
    }
    const sdm = li.querySelector("input[data-sdm]");
    if (sdm) {
      // clicking/typing in the input must not start a card drag
      sdm.onmousedown = (e) => { e.stopPropagation(); li.draggable = false; };
      sdm.onblur = () => { li.draggable = true; };
      sdm.onchange = () => {
        const v = parseInt(sdm.value, 10);
        if (!(v >= 1 && v <= 10)) {
          return toast("lesson threshold must be 1-10", true);
        }
        putObjSettings({ single_day_max: v });
      };
    }
    return li;
  }

  function renderObjList() {
    const hardUl = $("#hard-objs", ctrl);
    const prioUl = $("#prio-list", ctrl);
    hardUl.innerHTML = "";
    prioUl.innerHTML = "";
    for (const key of state.objOrder.filter(k => k in caps)) {
      hardUl.append(makeCard(key, 0));
    }
    state.objOrder.filter(k => !(k in caps)).forEach((key, i) => {
      prioUl.append(makeCard(key, i + 1));
    });
  }
  renderObjList();

  // drop zones for the empty ends of the two halves and the divider
  const hardZone = $("#hard-zone", ctrl);
  const divider = $("#obj-divider", ctrl);
  const prioUl = $("#prio-list", ctrl);
  const hardUl = $("#hard-objs", ctrl);
  function zoneDrop(target, opts, showEdge) {
    target.ondragover = (e) => {
      if (!dragKey || e.target.closest(".prio-item")) return;
      e.preventDefault();
      showEdge();
    };
    target.ondrop = (e) => {
      if (e.target.closest(".prio-item")) return;   // handled by the card
      e.preventDefault();
      clearDropMarks();
      const moved = dragKey || e.dataTransfer.getData("text/plain");
      dragKey = null;
      if (moved) settle(moved, opts);
    };
  }
  // above -> priority 0 (inserted first among the rank-0 cards)
  zoneDrop(hardZone, { hard: true }, () => markListStart(hardUl));
  // onto the divider -> first priority
  zoneDrop(divider, { hard: false, atStart: true },
           () => markListStart(prioUl));
  // empty space below -> last priority
  zoneDrop(prioUl, { hard: false }, () => markListEnd(prioUl));

  // persistent outcome of the LAST generate run (a toast is transient;
  // the user must be able to see whether the exact optimizer actually
  // found something within its budget)
  const V2_MSGS = {
    optimal: ["ok", "The exact optimizer PROVED this is the best "
      + "possible schedule for the current rules and priorities."],
    improved: ["ok", "The exact optimizer found a better schedule than "
      + "the one you started from. The budget ran out before proving "
      + "it optimal — a bigger budget might improve it further."],
    no_improvement: ["warn", "The exact optimizer searched its whole "
      + "budget but found nothing better than the standard solver's "
      + "schedule. A bigger budget might do better."],
    kept_current: ["warn", "The exact optimizer found nothing better "
      + "than the schedule you already had — it was kept unchanged. A "
      + "bigger budget might do better."],
    kept_v1: ["warn", "The exact optimizer found nothing better within "
      + "its budget — the standard solver's schedule was kept. A bigger "
      + "budget might do better."],
    no_solution_in_budget: ["warn", "The exact optimizer ran out of "
      + "budget before finding any usable schedule, so the best "
      + "schedule already known was kept (see above). Try a bigger "
      + "budget; if the Status panel reports always-active rule "
      + "violations, those rules may be too strict to satisfy at all."],
    infeasible: ["bad", "No schedule can satisfy all the rules: the "
      + "always-active bounds are impossible with the current inputs — "
      + "relax an always-active bound or adjust the inputs. The best "
      + "schedule already known was kept (see above)."],
    invalid_output: ["warn", "The exact optimizer produced an invalid "
      + "schedule — the best schedule already known was kept."],
    unavailable: ["warn", "The exact optimizer is unavailable (the "
      + "ortools package is not installed) — the standard solver was "
      + "used."],
    input_problem: ["warn", "Input problems prevent exact optimization "
      + "— the standard solver's result is shown."],
  };
  const BACKEND_LABEL = { cpsat: "exact optimizer",
                          v1: "standard solver",
                          current: "your previous schedule, kept" };
  if (state.lastGen) {
    const res = state.lastGen;
    const head = res.complete
      ? `Last run: complete schedule — ${res.scheduled} lessons (`
        + (BACKEND_LABEL[res.backend] || res.backend) + ")."
      : `Last run: partial schedule — ${res.scheduled} lessons placed, `
        + `${(res.unscheduled || []).length} need(s) unscheduled.`;
    const box = el(`<div class="gen-outcome"><div>${esc(head)}</div></div>`);
    const [kind, msg] = V2_MSGS[res.v2_outcome] || [];
    if (msg) {
      const cls = kind === "bad" ? "violation"
        : kind === "warn" ? "warning" : "gen-ok";
      box.append(el(`<div class="${cls}">${esc(msg)}</div>`));
    }
    $("#gen-result", ctrl).append(box);
  }

  $("#gen", ctrl).onclick = async () => {
    const btn = $("#gen", ctrl);
    state.keep = $("#opt-keep", ctrl).checked;
    state.compress = $("#opt-compress", ctrl).checked;
    state.exact = $("#opt-exact", ctrl).checked;
    const budget = parseInt($("#opt-exact-budget", ctrl).value, 10);
    if (state.exact && !(budget >= 1 && budget <= 600)) {
      return toast("search budget must be between 1 and 600 seconds", true);
    }
    state.exactBudget = budget || state.exactBudget;
    btn.disabled = true;
    btn.textContent = "Solving…";

    // live progress: a countdown bar for the exact solver (its search
    // budget is known), an elapsed indicator for the standard one
    const known = state.exact ? state.exactBudget : null;
    const prog = el(`<div class="gen-progress${known ? "" : " indeterminate"}">
      <div class="gen-progress-track"><div class="gen-progress-bar"></div></div>
      <div class="gen-progress-label">Solving…</div></div>`);
    $("#gen-result", ctrl).replaceChildren(prog);
    const barEl = $(".gen-progress-bar", prog);
    const labEl = $(".gen-progress-label", prog);
    const t0 = Date.now();
    const tick = () => {
      const elapsed = (Date.now() - t0) / 1000;
      if (known) {
        const remain = known - elapsed;
        if (remain > 0) {
          barEl.style.width = `${Math.min(100, elapsed / known * 100)}%`;
          labEl.textContent =
            `Solving… about ${Math.ceil(remain)} s remaining`;
        } else {
          barEl.style.width = "100%";
          prog.classList.add("overtime");
          labEl.textContent = "Solving… finishing up";
        }
      } else if (elapsed >= 1.5) {
        labEl.textContent = `Solving… ${Math.floor(elapsed)} s elapsed`;
      }
    };
    tick();
    const timer = setInterval(tick, 250);

    try {
      const res = await api("POST", "/api/schedule/generate", {
        keep_existing: state.keep,
        compress_teacher_days: state.compress,
        solver: state.exact ? "v2" : "v1",
        v2_time_budget: state.exactBudget,
        objective_order: state.objOrder,
      });
      state.lastGen = res;
      if (res.complete) {
        toast(`Complete schedule: ${res.scheduled} lessons`
          + (state.exact ? ` (${res.backend})` : ""));
      } else toast("Partial schedule — see unscheduled list", true);
      render();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = "Generate schedule";
      prog.remove();
    } finally {
      clearInterval(timer);
    }
  };
  $("#opt-keep", ctrl).onchange = (e) => { state.keep = e.target.checked; };
  $("#opt-compress", ctrl).onchange = (e) => { state.compress = e.target.checked; };
  function pickSolver(exact) {
    state.exact = exact;
    $("#exact-opts", ctrl).hidden = !exact;
    $("#opt-compress", ctrl).disabled = exact;
    $("#compress-note", ctrl).hidden = !exact;
  }
  $("#opt-standard", ctrl).onchange = () => pickSolver(false);
  $("#opt-exact", ctrl).onchange = () => pickSolver(true);
  $("#opt-exact-budget", ctrl).onchange = (e) => {
    const v = parseInt(e.target.value, 10);
    if (v >= 1 && v <= 600) state.exactBudget = v;
  };
  $("#clear", ctrl).onclick = async () => {
    if (!await appConfirm("Delete all scheduled lessons? (🔒 locked "
      + "lessons are kept)", "Delete all")) return;
    try {
      const res = await api("DELETE", "/api/schedule");
      if (res.kept_locked) {
        toast(`Cleared ${res.deleted} lessons — ${res.kept_locked} locked lesson(s) kept`);
      }
    } catch (e) { toast(e.message, true); }
    render();
  };

  const status = el(`<div class="panel"><h2>Status</h2>
    <div class="row">
      <label title="When off, conflicting additions and moves are saved
        immediately without asking — they are still reported here.">
        <input type="checkbox" id="opt-caution"${state.caution ? " checked" : ""}>
        ask for confirmation before saving a change that breaks constraints
      </label>
    </div></div>`);
  $("#opt-caution", status).onchange = (e) => { state.caution = e.target.checked; };
  if (check.problems.length) {
    for (const p of check.problems) status.append(el(`<div class="warning">⚠ ${esc(p)}</div>`));
  }
  if (schedule.violations.length) {
    for (const v of schedule.violations)
      status.append(el(`<div class="violation">✗ ${esc(v.message)}</div>`));
  }
  const unmet = schedule.coverage;
  for (const c of unmet) status.append(el(`<div class="warning">△ ${esc(c.message)}</div>`));
  if (!check.problems.length && !schedule.violations.length && !unmet.length) {
    status.append(el(`<div class="okmsg">✓ Schedule is valid and every need is covered.</div>`));
  }
  // quick checks for the soft objectives: teacher workload and the
  // one-lesson-per-day-per-student rule
  if (schedule.lessons.length &&
      (schedule.teacher_stats.length || schedule.student_stats.length)) {
    const o = schedule.objective;
    const wl = el(`<div class="workload"><div class="workload-tables">
      <table><thead><tr><th>Teacher</th><th>Lessons</th>
        <th>Working days</th></tr></thead><tbody>${
        schedule.teacher_stats.map(t =>
          `<tr><td><span class="tcolor-dot"
             style="background:${teacherColor(t.teacher_id)}"></span><button
             class="person-link" data-kind="teacher"
             data-pid="${esc(t.teacher_id)}" title="show in the timetable
             filter">${esc(t.name)}</button></td><td>${t.lessons}</td>
           <td>${t.days}</td></tr>`).join("")
      }</tbody></table>
      <table><thead><tr><th>Student</th><th>Lessons</th>
        <th>Lesson days</th><th>Two-lesson days</th></tr></thead><tbody>${
        schedule.student_stats.map(s =>
          `<tr${s.double_days.length ? ' class="double-day"' : ""}>
           <td><button class="person-link" data-kind="student"
             data-pid="${esc(s.student_id)}" title="show in the timetable
             filter">${esc(s.name)}</button></td>
           <td>${s.lessons}</td><td>${s.days}</td>
           <td>${s.double_days.length
              ? `${s.double_days.length} (${s.double_days.map(fmtDate).join(", ")})`
              : "—"}</td></tr>`).join("")
      }</tbody></table></div>
      <p class="muted">Student days with two lessons: ${o.student_double_days} ·
        with non-consecutive lessons: ${o.student_day_gaps} ·
        assigned-teacher miss points: ${o.pair_miss} ·
        lesson-count spread between teachers (max−min): ${o.slot_spread} ·
        total teacher working days: ${o.total_days} ·
        teacher days with ≤${settings.single_day_max}
        lesson${settings.single_day_max > 1 ? "s" : ""}: ${o.teacher_single_days} ·
        day-count spread: ${o.day_spread}</p></div>`);
    status.append(wl);
  }

  // manual add
  const opt = (arr, label) => arr.map(x =>
    `<option value="${esc(x.id)}">${esc(label(x))}</option>`).join("");
  const add = el(`<div class="panel"><h2>Add lesson manually</h2>
    <div class="row">
      <select id="l-student">${opt(students, s => s.name)}</select>
      <select id="l-subject">${opt(subjects, s => s.name)}</select>
      <select id="l-teacher">${opt(teachers, t => t.name)}</select>
      <select id="l-room">${opt(rooms, r => r.name)}</select>
      <select id="l-slot">${opt(sorted, slotLabel)}</select>
      <button class="action" id="add-lesson">Add</button>
    </div>
    <p class="muted">Additions that break a constraint are rejected with an
      explanation; confirm to override.</p></div>`);
  // keep the last-used selections across adds and re-renders, so
  // consecutive entries continue from the previous ones
  const setSel = (sel, val) => {
    if (sel && val && [...sel.options].some((o) => o.value === val)) {
      sel.value = val;
    }
  };
  if (state.addSel) {
    setSel($("#l-student", add), state.addSel.student_id);
    setSel($("#l-subject", add), state.addSel.subject_id);
    setSel($("#l-teacher", add), state.addSel.teacher_id);
    setSel($("#l-room", add), state.addSel.room_id);
    setSel($("#l-slot", add), state.addSel.timeslot_id);
  }
  $("#add-lesson", add).onclick = async () => {
    const body = {
      student_id: $("#l-student", add).value,
      subject_id: $("#l-subject", add).value,
      teacher_id: $("#l-teacher", add).value,
      room_id: $("#l-room", add).value,
      timeslot_id: $("#l-slot", add).value,
    };
    state.addSel = { ...body };
    if (!state.caution) {
      try {
        const res = await api("POST", "/api/lessons", { ...body, force: true });
        if (res.violations.length) {
          toast(`Added with ${res.violations.length} violation(s) — see Status`, true);
        }
        render();
      } catch (e) { toast(e.message, true); }
      return;
    }
    try {
      await api("POST", "/api/lessons", body);
      render();
    } catch (e) {
      if (await appConfirm(`This lesson breaks constraints:\n\n${e.message}`, "Add anyway")) {
        try { await api("POST", "/api/lessons", { ...body, force: true }); render(); }
        catch (e2) { toast(e2.message, true); }
      }
    }
  };

  // calendar of all lessons: delete buttons, violation highlighting, and
  // drag-and-drop between timeslots
  // lesson id -> the violation messages it is involved in, so a red
  // card can explain itself on hover
  const badMsgs = {};
  for (const v of schedule.violations) {
    for (const id of v.lesson_ids || []) {
      if (id != null) (badMsgs[id] ??= []).push(v.message);
    }
  }
  const totalLessons = schedule.lessons.length;
  // prune selection of lessons that no longer exist
  const liveIds = new Set(schedule.lessons.map(l => l.id));
  state.selectedLessons = new Set(
    [...state.selectedLessons].filter(id => liveIds.has(id)));
  const nSel = state.selectedLessons.size;

  const grid = el(`<div class="panel${state.selectMode ? " selecting" : ""}">
    <div class="tt-head"><h2>Timetable
      <span class="muted" id="lesson-count">(${totalLessons} lessons)</span></h2>
      <button class="action secondary tt-undo" id="undo-btn"${
        (schedule.undo || {}).count ? "" : " disabled"} title="${
        (schedule.undo || {}).count
          ? `undo: ${esc(schedule.undo.label)}` : "nothing to undo"
      }">↩ Undo</button></div>
    <p class="muted">Drag a lesson card onto another timeslot to move it.
      Moves that break a constraint are rejected with an explanation;
      confirm to override.</p>
    <div class="row select-bar">
      <button class="action secondary" id="sel-mode">${
        state.selectMode ? "✓ selecting — click lessons" : "Select lessons…"
      }</button>
      ${state.selectMode || nSel ? `
        <span id="sel-count"${nSel ? "" : ' class="muted"'}>${nSel} selected</span>
        <label class="gen-inline">repeat over the next
          <input type="number" id="rep-weeks" min="1" max="12"
            value="${state.repeatWeeks}" style="width:4rem"> week(s)</label>
        <button class="action" id="rep-go"${nSel ? "" : " disabled"}>Repeat</button>
        <button class="action secondary" id="sel-clear"${nSel ? "" : " disabled"}>Clear selection</button>
      ` : ""}
    </div>
    ${state.selectMode || nSel ? `
    <div class="row select-bar">
      <span class="muted">change selected to:</span>
      <select id="bulk-subject"><option value="">(keep subject)</option>
        ${opt(subjects, s => s.name)}</select>
      <select id="bulk-teacher"><option value="">(keep teacher)</option>
        ${opt(teachers, t => t.name)}</select>
      <select id="bulk-room"><option value="">(keep room)</option>
        ${opt(rooms, r => r.name)}</select>
      <button class="action" id="bulk-go"${nSel ? "" : " disabled"}>Apply</button>
    </div>` : ""}</div>`);
  $("#undo-btn", grid).onclick = async () => {
    try {
      const res = await api("POST", "/api/schedule/undo");
      toast(`Undid: ${res.undid}`);
    } catch (e) { toast(e.message, true); }
    render();
  };
  // leaving select mode KEEPS the selection (it survives re-entering
  // the mode, tab switches, filters…); only "Clear selection" — or the
  // selected lessons disappearing — resets it
  $("#sel-mode", grid).onclick = () => {
    state.selectMode = !state.selectMode;
    render();
  };
  const selClear = $("#sel-clear", grid);
  if (selClear) {
    selClear.onclick = () => { state.selectedLessons.clear(); render(); };
  }
  const repGo = $("#rep-go", grid);
  const bulkGo = $("#bulk-go", grid);
  const selCount = $("#sel-count", grid);
  const updateSelBar = () => {
    const n = state.selectedLessons.size;
    if (selCount) {
      selCount.textContent = `${n} selected`;
      selCount.classList.toggle("muted", !n);
    }
    if (repGo) repGo.disabled = !n;
    if (bulkGo) bulkGo.disabled = !n;
    if (selClear) selClear.disabled = !n;
  };
  if (bulkGo) {
    bulkGo.onclick = async () => {
      const body = { lesson_ids: [...state.selectedLessons] };
      for (const [field, sel] of [["subject_id", "#bulk-subject"],
                                  ["teacher_id", "#bulk-teacher"],
                                  ["room_id", "#bulk-room"]]) {
        const v = $(sel, grid).value;
        if (v) body[field] = v;
      }
      if (!body.subject_id && !body.teacher_id && !body.room_id) {
        return toast("choose a subject, teacher or room to change", true);
      }
      const report = (res) => {
        let msg = `Updated ${res.updated} lesson(s)`;
        if (res.skipped_locked) {
          msg += ` · ${res.skipped_locked} locked lesson(s) skipped`;
        }
        toast(msg, res.updated === 0);
      };
      try {
        report(await api("POST", "/api/lessons/bulk_update", body));
        render();
      } catch (e) {
        if (await appConfirm(
          `This change breaks constraints:\n\n${e.message}`,
          "Change anyway")) {
          try {
            report(await api("POST", "/api/lessons/bulk_update",
              { ...body, force: true }));
            render();
          } catch (e2) { toast(e2.message, true); }
        }
      }
    };
  }
  if (repGo) {
    repGo.onclick = async () => {
      const weeks = parseInt($("#rep-weeks", grid).value, 10);
      if (!(weeks >= 1 && weeks <= 12)) {
        return toast("weeks must be between 1 and 12", true);
      }
      state.repeatWeeks = weeks;
      const body = { lesson_ids: [...state.selectedLessons], weeks };
      const report = (res) => {
        let msg = `Created ${res.created} lesson(s)`;
        if (res.skipped_duplicate) {
          msg += ` · ${res.skipped_duplicate} already existed`;
        }
        if (res.skipped_no_slot) {
          msg += ` · ${res.skipped_no_slot} skipped (no matching timeslot)`;
        }
        toast(msg, res.created === 0);
      };
      try {
        report(await api("POST", "/api/lessons/repeat", body));
        render();
      } catch (e) {
        if (await appConfirm(
          `Repeating breaks constraints:\n\n${e.message}`, "Repeat anyway")) {
          try {
            report(await api("POST", "/api/lessons/repeat",
              { ...body, force: true }));
            render();
          } catch (e2) { toast(e2.message, true); }
        }
      }
    };
  }

  // ---- visibility filter: toggle teachers / students on and off
  const filterActive = state.hiddenTeachers.size || state.hiddenStudents.size;
  const filterBox = el(`<details class="filter-box"${filterActive ? " open" : ""}>
    <summary>Filter <span class="muted" id="filter-note"></span></summary>
    <div class="filter-sort">sort by
      <select id="filter-sort">
        <option value="name">name</option>
        <option value="id">ID</option>
        <option value="lessons">lesson count</option>
      </select></div>
    <div class="filter-groups"></div></details>`);
  const groupsEl = $(".filter-groups", filterBox);
  $("#filter-sort", filterBox).value = state.filterSort;

  const lessonCount = { teacher: {}, student: {} };
  for (const l of schedule.lessons) {
    lessonCount.teacher[l.teacher_id] =
      (lessonCount.teacher[l.teacher_id] || 0) + 1;
    lessonCount.student[l.student_id] =
      (lessonCount.student[l.student_id] || 0) + 1;
  }

  function sortedPeople(people, kind) {
    const arr = [...people];
    if (state.filterSort === "id") {
      arr.sort((a, b) => a.id.localeCompare(b.id, undefined, {numeric: true}));
    } else if (state.filterSort === "lessons") {
      arr.sort((a, b) =>
        (lessonCount[kind][b.id] || 0) - (lessonCount[kind][a.id] || 0)
        || a.name.localeCompare(b.name));
    } else {
      arr.sort((a, b) => a.name.localeCompare(b.name) ||
                         a.id.localeCompare(b.id));
    }
    return arr;
  }

  function applyFilter() {
    const ht = state.hiddenTeachers, hs = state.hiddenStudents;
    let shown = 0;
    for (const box of grid.querySelectorAll(".cal-entry[data-teacher-id]")) {
      let any = false;
      for (const card of box.querySelectorAll(".lesson-card")) {
        const hide = ht.has(box.dataset.teacherId)
          || hs.has(card.dataset.studentId);
        card.classList.toggle("filter-hidden", hide);
        if (!hide) { any = true; shown++; }
      }
      box.classList.toggle("filter-hidden", !any);
    }
    const active = ht.size || hs.size;
    $("#lesson-count", grid).textContent = active
      ? `(showing ${shown} of ${totalLessons} lessons)`
      : `(${totalLessons} lessons)`;
    $("#filter-note", filterBox).textContent = active
      ? "— some lessons are hidden" : "";
  }

  function chipGroup(label, people, hiddenSet, kind) {
    const row = el(`<div class="filter-row"><span class="filter-label">
      ${label}</span><span class="filter-chips"></span>
      <span class="filter-quick">
        <button class="small" data-q="all">all</button>
        <button class="small" data-q="none">none</button></span></div>`);
    const chipsEl = $(".filter-chips", row);
    const chips = [];
    for (const p of sortedPeople(people, kind)) {
      const n = lessonCount[kind][p.id] || 0;
      const countTag = state.filterSort === "lessons"
        ? ` <span class="chip-count">${n}</span>` : "";
      const chip = el(`<button class="chip${hiddenSet.has(p.id) ? " off" : ""}"
        data-kind="${kind}" data-pid="${esc(p.id)}"
        title="${n} lesson(s) — click to show/hide">${esc(p.name)}${countTag}</button>`);
      chip.onclick = () => {
        if (hiddenSet.has(p.id)) hiddenSet.delete(p.id);
        else hiddenSet.add(p.id);
        chip.classList.toggle("off", hiddenSet.has(p.id));
        applyFilter();
      };
      chips.push([p.id, chip]);
      chipsEl.append(chip);
    }
    row.querySelector("[data-q='all']").onclick = () => {
      for (const [id, chip] of chips) {
        hiddenSet.delete(id);
        chip.classList.remove("off");
      }
      applyFilter();
    };
    row.querySelector("[data-q='none']").onclick = () => {
      for (const [id, chip] of chips) {
        hiddenSet.add(id);
        chip.classList.add("off");
      }
      applyFilter();
    };
    return row;
  }

  function renderChipGroups() {
    groupsEl.replaceChildren(
      chipGroup("Teachers", teachers, state.hiddenTeachers, "teacher"),
      chipGroup("Students", students, state.hiddenStudents, "student"));
  }
  renderChipGroups();
  $("#filter-sort", filterBox).onchange = (e) => {
    state.filterSort = e.target.value;
    renderChipGroups();
  };
  grid.append(filterBox);

  // Shared caution flow for any lesson change (drag-move or inline edit).
  async function patchLesson(lessonId, fields, verb) {
    if (!state.caution) {
      try {
        const res = await api("PATCH", `/api/lessons/${lessonId}`,
          { ...fields, force: true });
        if (res.violations.length) {
          toast(`${verb} with ${res.violations.length} violation(s) — see Status`, true);
        }
        render();
      } catch (e) { toast(e.message, true); }
      return;
    }
    try {
      await api("PATCH", `/api/lessons/${lessonId}`, fields);
      render();
    } catch (e) {
      if (await appConfirm(`This change breaks constraints:\n\n${e.message}`, "Save anyway")) {
        try {
          await api("PATCH", `/api/lessons/${lessonId}`,
            { ...fields, force: true });
          render();
        } catch (e2) { toast(e2.message, true); }
      }
    }
  }
  const moveLesson = (lessonId, timeslotId) =>
    patchLesson(lessonId, { timeslot_id: timeslotId }, "Moved");

  // inline add: a small ＋ on each slot block (and right-click on the
  // slot) opens a mini form pre-targeted at that timeslot
  const openSlotAdd = (block, slotId) => {
    const prev = $(".slot-add", grid);
    if (prev) prev.remove();
    const form = el(`<div class="lesson-card slot-add">
      <div class="lesson-edit">
        <select id="a-student">${opt(students, s => s.name)}</select>
        <select id="a-subject">${opt(subjects, s => s.name)}</select>
        <select id="a-teacher">${opt(teachers, t => t.name)}</select>
        <select id="a-room">${opt(rooms, r => r.name)}</select>
      </div>
      <div class="lesson-edit-actions">
        <button class="action" id="a-save">Add</button>
        <button class="action secondary" id="a-cancel">Cancel</button>
      </div></div>`);
    const setSel = (sel, val) => {
      if (sel && val && [...sel.options].some((o) => o.value === val)) {
        sel.value = val;
      }
    };
    if (state.addSel) {
      setSel($("#a-student", form), state.addSel.student_id);
      setSel($("#a-subject", form), state.addSel.subject_id);
      setSel($("#a-teacher", form), state.addSel.teacher_id);
      setSel($("#a-room", form), state.addSel.room_id);
    }
    $("#a-cancel", form).onclick = () => form.remove();
    $("#a-save", form).onclick = async () => {
      const body = {
        student_id: $("#a-student", form).value,
        subject_id: $("#a-subject", form).value,
        teacher_id: $("#a-teacher", form).value,
        room_id: $("#a-room", form).value,
        timeslot_id: slotId,
      };
      state.addSel = { ...body };
      state.reopenSlotAdd = slotId;   // keep the flow going after add
      if (!state.caution) {
        try {
          const res = await api("POST", "/api/lessons",
            { ...body, force: true });
          if (res.violations.length) {
            toast(`Added with ${res.violations.length} violation(s) — `
              + "see Status", true);
          }
          render();
        } catch (e) {
          state.reopenSlotAdd = null;
          toast(e.message, true);
        }
        return;
      }
      try {
        await api("POST", "/api/lessons", body);
        render();
      } catch (e) {
        if (await appConfirm(
          `This lesson breaks constraints:\n\n${e.message}`,
          "Add anyway")) {
          try {
            await api("POST", "/api/lessons", { ...body, force: true });
            render();
          } catch (e2) {
            state.reopenSlotAdd = null;
            toast(e2.message, true);
          }
        } else {
          state.reopenSlotAdd = null;
        }
      }
    };
    block.append(form);
    $("#a-student", form).focus();
  };

  const dropHook = (block, slot) => {
    block.dataset.slotId = slot.timeslot_id;
    block.ondragover = (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      block.classList.add("drop-target");
    };
    block.ondragleave = () => block.classList.remove("drop-target");
    block.ondrop = (e) => {
      e.preventDefault();
      block.classList.remove("drop-target");
      const lessonId = e.dataTransfer.getData("text/plain");
      if (lessonId) moveLesson(lessonId, slot.timeslot_id);
    };
    const addBtn = el(`<button class="slot-add-btn"
      title="add a lesson in this timeslot">＋</button>`);
    addBtn.onclick = (e) => {
      e.stopPropagation();
      openSlotAdd(block, slot.timeslot_id);
    };
    block.prepend(addBtn);
    block.oncontextmenu = (e) => {
      e.preventDefault();
      openSlotAdd(block, slot.timeslot_id);
    };
  };

  if (!overview.weeks.length) {
    grid.append(el(`<p class="muted">No timeslots defined yet.</p>`));
  } else {
    // swap a card's content for subject/teacher/room dropdowns + save/cancel,
    // with live ✓/✗ constraint feedback per option and for the combination
    function openEditor(card, l, entryTeacherId) {
      card.draggable = false;
      card.classList.add("editing");
      card.innerHTML = "";
      const sel = (id, items, current) => `<select id="${id}">${items.map(x =>
        `<option value="${esc(x.id)}" data-name="${esc(x.name)}"${
          x.id === current ? " selected" : ""}>${esc(x.name)}</option>`).join("")}
        </select>`;
      const form = el(`<div class="lesson-edit">
        <div class="muted">${esc(l.student_name)}</div>
        ${sel("e-subject", subjects, l.subject_id)}
        ${sel("e-teacher", teachers, entryTeacherId)}
        ${sel("e-room", rooms, l.room_id)}
        <div class="lesson-edit-check muted">checking…</div>
        <div class="lesson-edit-actions">
          <button class="action" id="e-save">Save</button>
          <button class="action secondary" id="e-cancel">Cancel</button>
        </div></div>`);
      const checkBox = $(".lesson-edit-check", form);

      let seq = 0;
      async function refresh() {
        const mySeq = ++seq;
        let res;
        try {
          res = await api("POST", `/api/lessons/${l.lesson_id}/check_options`, {
            subject_id: $("#e-subject", form).value,
            teacher_id: $("#e-teacher", form).value,
            room_id: $("#e-room", form).value,
          });
        } catch (e) { checkBox.textContent = e.message; return; }
        if (mySeq !== seq) return;   // a newer selection superseded this one
        for (const [selId, key] of [["e-subject", "subjects"],
                                    ["e-teacher", "teachers"],
                                    ["e-room", "rooms"]]) {
          for (const opt of $(`#${selId}`, form).options) {
            const bad = (res[key][opt.value] || []).length > 0;
            opt.textContent = `${bad ? "✗" : "✓"} ${opt.dataset.name}`;
            opt.title = (res[key][opt.value] || []).join("\n");
          }
        }
        checkBox.innerHTML = "";
        if (res.current.length) {
          for (const m of res.current) {
            checkBox.append(el(`<div class="edit-bad">✗ ${esc(m)}</div>`));
          }
        } else {
          checkBox.append(el(`<div class="edit-ok">✓ constraints met</div>`));
        }
      }
      for (const id of ["e-subject", "e-teacher", "e-room"]) {
        $(`#${id}`, form).onchange = refresh;
      }

      $("#e-save", form).onclick = () => patchLesson(l.lesson_id, {
        subject_id: $("#e-subject", form).value,
        teacher_id: $("#e-teacher", form).value,
        room_id: $("#e-room", form).value,
      }, "Saved");
      $("#e-cancel", form).onclick = () => render();
      card.append(form);
      refresh();
    }

    const lockedIds = new Set(
      schedule.lessons.filter(l => l.locked).map(l => l.id));
    grid.append(calendarTable(overview, (entry) => {
      const box = el(`<div class="cal-entry" data-teacher-id="${entry.teacher_id}"
        style="border-left-color:${teacherColor(entry.teacher_id)}">
        <b>${esc(entry.teacher_name)}</b></div>`);
      for (const l of entry.lessons) {
        const locked = lockedIds.has(l.lesson_id);
        const selected = state.selectedLessons.has(l.lesson_id);
        // locked lessons can't be dragged, edited or deleted — only the
        // lock button stays active, so a stray drag can't move them.
        // In select mode dragging is off for everyone: clicks select.
        const bad = badMsgs[l.lesson_id];
        const card = el(`<div class="lesson-card${bad ? " bad" : ""}${locked ? " locked" : ""}${selected ? " selected" : ""}"
          ${bad ? `title="${esc("⚠ " + bad.join("\n⚠ "))}"` : ""}
          draggable="${locked || state.selectMode ? "false" : "true"}" data-lesson-id="${l.lesson_id}"
          data-student-id="${l.student_id}">
          ${esc(l.student_name)} — ${esc(l.subject_name)}
          <span class="muted">· ${esc(l.room_name)}</span>
          <button class="lock" title="${locked
            ? "locked in place — click to unlock"
            : "lock in place (survives generate and clear)"}">${locked ? "🔒" : "🔓"}</button>
          ${locked ? "" : `<button class="edit" title="edit teacher / room / subject">✎</button>
          <button class="del" title="delete">×</button>`}</div>`);
        if (state.selectMode) {
          card.onclick = (e) => {
            if (e.target.closest("button")) return;
            if (state.selectedLessons.has(l.lesson_id)) {
              state.selectedLessons.delete(l.lesson_id);
            } else {
              state.selectedLessons.add(l.lesson_id);
            }
            card.classList.toggle("selected");
            updateSelBar();
          };
        }
        if (!locked) {
          if (!state.selectMode) {
            card.ondragstart = (e) => {
              e.dataTransfer.setData("text/plain", String(l.lesson_id));
              e.dataTransfer.effectAllowed = "move";
            };
          }
          $(".edit", card).onclick = () =>
            openEditor(card, l, entry.teacher_id);
          $(".del", card).onclick = async () => {
            await api("DELETE", `/api/lessons/${l.lesson_id}`).catch(e => toast(e.message, true));
            render();
          };
        }
        $(".lock", card).onclick = async () => {
          try {
            await api("POST", `/api/lessons/${l.lesson_id}/lock`,
              { locked: !locked });
          } catch (e) { toast(e.message, true); }
          render();
        };
        box.append(card);
      }
      return box;
    }, dropHook));
    applyFilter();
    if (state.reopenSlotAdd) {
      const target = grid.querySelector(
        `.cal-slot[data-slot-id="${CSS.escape(state.reopenSlotAdd)}"]`);
      state.reopenSlotAdd = null;
      if (target) openSlotAdd(target, target.dataset.slotId);
    }

    // rubber-band selection (select mode): drag across the timetable
    // to add every touched lesson card to the selection
    let bandSuppress = false;
    grid.addEventListener("mousedown", (e) => {
      if (!state.selectMode || e.button !== 0) return;
      if (e.target.closest("button, select, input, .slot-add")) return;
      if (!e.target.closest(".cal-table")) return;
      const docXY = (ev) => [ev.clientX + window.scrollX,
                             ev.clientY + window.scrollY];
      const start = docXY(e);
      let band = null;
      // cache card boxes in document coordinates once, so live hit
      // testing is pure math even while the page auto-scrolls
      const cards = [...grid.querySelectorAll(
        ".lesson-card[data-lesson-id]")].map((c) => {
        const r = c.getBoundingClientRect();
        return { el: c, id: +c.dataset.lessonId,
                 l: r.left + window.scrollX, t: r.top + window.scrollY,
                 r: r.right + window.scrollX,
                 b: r.bottom + window.scrollY };
      });
      const rectOf = (cur) => [
        Math.min(start[0], cur[0]), Math.min(start[1], cur[1]),
        Math.max(start[0], cur[0]), Math.max(start[1], cur[1])];
      const hits = (cur) => {
        const [x1, y1, x2, y2] = rectOf(cur);
        return cards.filter(c =>
          c.l < x2 && c.r > x1 && c.t < y2 && c.b > y1 && c.r > c.l);
      };
      const onMove = (ev) => {
        const cur = docXY(ev);
        if (!band) {
          if (Math.abs(cur[0] - start[0])
              + Math.abs(cur[1] - start[1]) < 6) return;
          band = el(`<div class="select-band"></div>`);
          document.body.append(band);
          document.body.classList.add("banding");
        }
        const [x1, y1, x2, y2] = rectOf(cur);
        Object.assign(band.style, {
          left: `${x1}px`, top: `${y1}px`,
          width: `${x2 - x1}px`, height: `${y2 - y1}px`,
        });
        const hit = new Set(hits(cur).map(c => c.id));
        for (const c of cards) {
          c.el.classList.toggle("selected",
            hit.has(c.id) || state.selectedLessons.has(c.id));
        }
        // scroll the page when the cursor nears the viewport edge
        const m = 45;
        if (ev.clientY < m) window.scrollBy(0, -22);
        else if (ev.clientY > window.innerHeight - m) {
          window.scrollBy(0, 22);
        }
      };
      const onUp = (ev) => {
        document.removeEventListener("mousemove", onMove);
        document.body.classList.remove("banding");
        if (band) {
          for (const c of hits(docXY(ev))) {
            state.selectedLessons.add(c.id);
          }
          updateSelBar();
          band.remove();
          bandSuppress = true;   // swallow the trailing click
        }
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp, { once: true });
    });
    grid.addEventListener("click", (e) => {
      if (bandSuppress) {
        e.stopPropagation();
        e.preventDefault();
        bandSuppress = false;
      }
    }, true);
  }

  // names in the Status workload tables jump to (and flash) the
  // person's chip in the timetable filter
  for (const link of status.querySelectorAll(".person-link")) {
    link.onclick = () => {
      filterBox.open = true;
      const chip = filterBox.querySelector(
        `.chip[data-kind="${link.dataset.kind}"]`
        + `[data-pid="${CSS.escape(link.dataset.pid)}"]`);
      if (!chip) return;
      chip.scrollIntoView({ behavior: "smooth", block: "center" });
      chip.focus({ preventScroll: true });
      // native-looking focus ring (same as keyboard Tab), kept until
      // the chip loses focus
      chip.classList.add("chip-target");
      chip.addEventListener("blur",
        () => chip.classList.remove("chip-target"), { once: true });
    };
  }

  // panel order: work area first (manual add + timetable), then the
  // review/config panels (status, generate)
  root.append(add);
  root.append(grid);
  root.append(status);
  root.append(ctrl);
}

// ----------------------------------------------------------------- calendars

const CAL_VIEWS = [
  ["overview", "Overview (teachers & students per slot)"],
  ["student", "Per student (their subjects)"],
  ["teacher", "Per teacher (subject & student)"],
];

function calEntryHtml(entry, view) {
  if (view === "overview") {
    return el(`<div class="cal-entry"
      style="border-left-color:${teacherColor(entry.teacher_id)}">
      <b>${esc(entry.teacher_name)}</b>${
      entry.lessons.map(l =>
        `<div class="cal-line">${esc(l.student_name)} — ${esc(l.subject_name)}
         <span class="muted">(${esc(l.room_name)})</span></div>`).join("")
    }</div>`);
  }
  if (view === "student") {
    return el(`<div class="cal-entry"
      style="border-left-color:${teacherColor(entry.teacher_id)}">
      <b>${esc(entry.subject_name)}</b>
      <div class="cal-line muted">${esc(entry.teacher_name)} · ${esc(entry.room_name)}</div></div>`);
  }
  return el(`<div class="cal-entry"
    style="border-left-color:${teacherColor(state.calPerson)}">
    <b>${esc(entry.subject_name)}</b>
    <div class="cal-line">${esc(entry.student_name)}
      <span class="muted">· ${esc(entry.room_name)}</span></div></div>`);
}

async function renderCalendars(root) {
  const view = state.calView;
  const [students, teachers] = await Promise.all([
    list("students"), list("teachers")]);
  const people = view === "student" ? students
    : view === "teacher" ? teachers : [];
  if (people.length && !people.some(p => p.id === state.calPerson)) {
    state.calPerson = people[0].id;
  }

  const ctrl = el(`<div class="panel no-print"><h2>Calendar views</h2>
    <div class="row">
      <select id="cal-view">${CAL_VIEWS.map(([k, label]) =>
        `<option value="${k}"${k === view ? " selected" : ""}>${label}</option>`).join("")}
      </select>
      ${people.length ? `<select id="cal-person">${people.map(p =>
        `<option value="${esc(p.id)}"${p.id === state.calPerson ? " selected" : ""}>
         ${esc(p.name)} (${esc(p.id)})</option>`).join("")}</select>` : ""}
      <button class="action secondary" id="cal-print">Print</button>
    </div></div>`);
  $("#cal-view", ctrl).onchange = (e) => { state.calView = e.target.value; render(); };
  const personSel = $("#cal-person", ctrl);
  if (personSel) personSel.onchange = (e) => { state.calPerson = e.target.value; render(); };
  $("#cal-print", ctrl).onclick = () => window.print();
  root.append(ctrl);

  let url = "/api/views/overview";
  let title = "All lessons";
  if (view === "student") {
    if (!people.length) {
      root.append(el(`<div class="panel"><p class="muted">No students yet.</p></div>`));
      return;
    }
    url = `/api/views/student/${encodeURIComponent(state.calPerson)}`;
  } else if (view === "teacher") {
    if (!people.length) {
      root.append(el(`<div class="panel"><p class="muted">No teachers yet.</p></div>`));
      return;
    }
    url = `/api/views/teacher/${encodeURIComponent(state.calPerson)}`;
  }
  const data = await api("GET", url);
  if (view === "student") title = `Schedule — ${data.student_name}`;
  if (view === "teacher") title = `Teaching schedule — ${data.teacher_name}`;
  if (!data.weeks.length) {
    root.append(el(`<div class="panel"><p class="muted">
      No timeslots defined yet.</p></div>`));
    return;
  }
  const panel = el(`<div class="panel cal-print"><h2>${esc(title)}</h2></div>`);
  panel.append(calendarTable(data, (entry) => calEntryHtml(entry, view)));
  root.append(panel);
}

// undo button for the grid tabs (same stack as the timetable's)
async function gridUndoButton() {
  const info = await api("GET", "/api/undo");
  const btn = el(`<button class="action secondary grid-undo"${
    info.count ? "" : " disabled"} title="${info.count
      ? `undo: ${esc(info.label)}` : "nothing to undo"}">↩ Undo</button>`);
  btn.onclick = async () => {
    try {
      const res = await api("POST", "/api/schedule/undo");
      toast(`Undid: ${res.undid}`);
    } catch (e) { toast(e.message, true); }
    render();
  };
  return btn;
}

// ---------------------------------------------------------------- assignments

async function renderAssignments(root) {
  const [students, teachers, pairs] = await Promise.all([
    list("students"), list("teachers"), list("teacher_students")]);
  const prio = new Map(
    pairs.map(p => [`${p.student_id}|${p.teacher_id}`, p.priority]));

  const panel = el(`<div class="panel">
    <div class="tt-head"><h2>Teacher in charge</h2></div>
    <p class="muted">Click a cell, then pick:
      <span class="pair-badge pair-hard">0</span> = the student
      <b>must</b> be taught by this teacher (hard rule);
      <span class="pair-badge pair-soft">1</span>
      <span class="pair-badge pair-soft">2</span>
      <span class="pair-badge pair-soft">3</span> = preferred, other
      teachers allowed but penalized (smaller number = stronger);
      ✕ = no assignment. The soft preference's rank among the other
      goals is the draggable "${esc(OBJ_LABELS.student_teacher_pair)}"
      card in the Generate panel.</p>
    <div class="grid-scroll"><table class="grid-table"><thead><tr>
      <th></th>${teachers.map(t =>
        `<th>${esc(t.name)}</th>`).join("")}</tr></thead>
    <tbody></tbody></table></div></div>`);
  let popEl = null;
  const closePop = () => { if (popEl) { popEl.remove(); popEl = null; } };
  panel.addEventListener("click", closePop);
  const tbody = $("tbody", panel);
  for (const st of students) {
    const tr = document.createElement("tr");
    tr.append(el(`<th>${esc(st.name)} (${esc(st.id)})</th>`));
    for (const t of teachers) {
      const key = `${st.id}|${t.id}`;
      const k = prio.get(key);
      const td = el(`<td class="pair-cell${
        k === 0 ? " pair-hard" : k !== undefined ? " pair-soft" : ""}">${
        k !== undefined ? k : "·"}</td>`);
      td.title = k === 0
        ? `${st.name} must be taught by ${t.name}`
        : k !== undefined
          ? `${t.name} preferred for ${st.name} (strength ${k})`
          : `click to assign ${t.name} to ${st.name}`;
      td.onclick = (ev) => {
        ev.stopPropagation();
        closePop();
        const pop = el(`<div class="pair-pop">
          <button class="pair-badge pair-hard${k === 0 ? " active" : ""}"
            data-v="0" title="must (hard rule)">0</button>
          ${[1, 2, 3].map(v =>
            `<button class="pair-badge pair-soft${k === v ? " active" : ""}"
              data-v="${v}" title="preferred (strength ${v})">${v}</button>`
          ).join("")}
          <button class="pair-badge pair-clear" data-v=""
            title="remove the assignment"${
            k === undefined ? " disabled" : ""}>✕</button></div>`);
        pop.onclick = async (e2) => {
          e2.stopPropagation();
          const b = e2.target.closest("button");
          if (!b || b.disabled) return;
          try {
            if (b.dataset.v === "") {
              await api("DELETE",
                `/api/teacher_students?teacher_id=${encodeURIComponent(t.id)}`
                + `&student_id=${encodeURIComponent(st.id)}`);
            } else {
              await api("POST", "/api/teacher_students",
                { teacher_id: t.id, student_id: st.id,
                  priority: parseInt(b.dataset.v, 10) });
            }
            render();
          } catch (e3) { toast(e3.message, true); render(); }
        };
        td.append(pop);
        // flip upward when the cell sits near the bottom of the
        // scrolling grid box, so the chooser is never clipped
        const box = td.closest(".grid-scroll");
        if (box) {
          const r = td.getBoundingClientRect();
          const c = box.getBoundingClientRect();
          if (r.bottom + 44 > c.bottom) pop.classList.add("up");
        }
        popEl = pop;
      };
      tr.append(td);
    }
    tbody.append(tr);
  }
  if (!students.length || !teachers.length) {
    root.append(el(`<div class="panel"><p class="muted">
      Add students and teachers first.</p></div>`));
    return;
  }
  enhanceGrid($(".grid-scroll", panel));
  $(".tt-head", panel).append(await gridUndoButton());

  // drag-select a rectangle -> assign/clear/copy/paste the whole block
  const bar = el(`<div class="row area-bar" hidden>
    <span class="muted area-count"></span>
    <span class="muted">set all to:</span>
    <button class="pair-badge pair-hard" data-prio="0">0</button>
    <button class="pair-badge pair-soft" data-prio="1">1</button>
    <button class="pair-badge pair-soft" data-prio="2">2</button>
    <button class="pair-badge pair-soft" data-prio="3">3</button>
    <button class="pair-badge pair-clear" data-prio="">✕</button>
    <button class="action secondary" data-act="copy">copy</button>
    <button class="action secondary" data-act="paste">paste</button>
    <button class="action secondary" data-act="unsel">deselect</button>
  </div>`);
  panel.insertBefore(bar, $(".grid-scroll", panel));
  let rect = null;
  const selApi = attachAreaSelect($("table", panel), (r) => {
    rect = r;
    bar.hidden = false;
    const n = (r.r2 - r.r1 + 1) * (r.c2 - r.c1 + 1);
    $(".area-count", bar).textContent = `${n} cells`;
    $('[data-act="paste"]', bar).disabled =
      !(state.gridClip && state.gridClip.kind === "pair");
  });
  bar.onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b || !rect) return;
    if (b.dataset.act === "unsel") {
      selApi.clear();
      rect = null;
      bar.hidden = true;
      return;
    }
    if (b.dataset.act === "copy") {
      const vals = [];
      for (let r = rect.r1; r <= rect.r2; r++) {
        const row = [];
        for (let c = rect.c1; c <= rect.c2; c++) {
          const k = prio.get(`${students[r].id}|${teachers[c].id}`);
          row.push(k === undefined ? null : k);
        }
        vals.push(row);
      }
      state.gridClip = { kind: "pair", vals };
      toast(`Copied ${vals.length} × ${vals[0].length} cells`);
      $('[data-act="paste"]', bar).disabled = false;
      return;
    }
    const body = { set: [], clear: [] };
    if (b.dataset.act === "paste") {
      const clip = state.gridClip;
      if (!clip || clip.kind !== "pair") return;
      for (let i = 0; i < clip.vals.length; i++) {
        for (let j = 0; j < clip.vals[i].length; j++) {
          const r = rect.r1 + i, c = rect.c1 + j;
          if (r >= students.length || c >= teachers.length) continue;
          const v = clip.vals[i][j];
          if (v === null) body.clear.push([teachers[c].id, students[r].id]);
          else body.set.push([teachers[c].id, students[r].id, v]);
        }
      }
    } else if ("prio" in b.dataset) {
      for (let r = rect.r1; r <= rect.r2; r++) {
        for (let c = rect.c1; c <= rect.c2; c++) {
          if (b.dataset.prio === "") {
            body.clear.push([teachers[c].id, students[r].id]);
          } else {
            body.set.push([teachers[c].id, students[r].id,
                           parseInt(b.dataset.prio, 10)]);
          }
        }
      }
    } else return;
    try {
      const res = await api("POST", "/api/teacher_students/bulk", body);
      toast(`Updated: ${res.set} assigned, ${res.cleared} cleared`);
      render();
    } catch (e2) { toast(e2.message, true); }
  };
  root.append(panel);
}

// -------------------------------------------------------------------- router

const RENDERERS = {
  schedule: renderSchedule,
  calendars: renderCalendars,
  students: (r) => renderNamedTable(r, "students", "Students"),
  teachers: renderTeachers,
  subjects: (r) => renderNamedTable(r, "subjects", "Subjects"),
  rooms: renderRooms,
  timeslots: renderTimeslots,
  needs: renderNeeds,
  assignments: renderAssignments,
  availability: renderAvailability,
  csv: renderCsv,
};

let _lastRenderedTab = null;

// Overlapping renders would each append their panels after the other's
// innerHTML clear, duplicating the page — serialize them instead:
// a render() during a render() coalesces into ONE follow-up pass.
let _renderRunning = false;
let _renderQueued = false;

async function render() {
  if (_renderRunning) { _renderQueued = true; return; }
  _renderRunning = true;
  try {
    do { _renderQueued = false; await _renderOnce(); } while (_renderQueued);
  } finally { _renderRunning = false; }
}

async function _renderOnce() {
  // Re-rendering the same tab (after a drag, edit, toggle, …) must not
  // jump the page back to the top; only a tab switch starts at the top.
  const sameTab = _lastRenderedTab === state.tab;
  const scrollY = window.scrollY;
  _lastRenderedTab = state.tab;

  const nav = $("#tabs");
  nav.innerHTML = "";
  for (const [key, label] of TABS) {
    const b = el(`<button class="${key === state.tab ? "active" : ""}">${label}</button>`);
    b.onclick = () => { state.tab = key; render(); };
    nav.append(b);
  }
  const root = $("#content");
  root.innerHTML = "";
  try {
    await RENDERERS[state.tab](root);
  } catch (e) {
    root.append(el(`<div class="panel"><div class="violation">${esc(e.message)}</div></div>`));
  }
  window.scrollTo(0, sameTab ? scrollY : 0);
}

render();
