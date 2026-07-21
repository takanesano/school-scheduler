"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const OBJ_LABELS = {
  student_double_day: "One lesson per day per student",
  student_day_gap: "Multiple lessons on a day must be consecutive",
  teacher_slot_spread: "Even lesson counts across teachers",
  teacher_working_day: "Few teacher working days",
  teacher_day_spread: "Even working-day counts across teachers",
};

const state = { tab: "schedule", keep: false, caution: true,
                compress: true, exact: false, exactBudget: 8,
                objOrder: Object.keys(OBJ_LABELS),
                hiddenTeachers: new Set(), hiddenStudents: new Set(),
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
  ["availability", "Availability"],
  ["csv", "CSV import/export"],
];

// ------------------------------------------------------------------ helpers

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
    <table><thead><tr><th>ID</th><th>Name</th><th>Can teach</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of teachers) {
    const taught = (subjectsOf[r.id] || [])
      .map(id => subname[id] || id).sort().join(", ");
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td>
      <td>${taught ? esc(taught) : '<span class="muted">— none yet —</span>'}</td>
      <td><button class="small">delete</button></td></tr>`);
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
      <div style="overflow-x:auto"><table class="grid-table"><thead><tr>
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
      <button class="action" id="add">Add / update</button>
    </div>
    <p class="muted">Capacity = how many simultaneous lessons fit in the room
      (e.g. booths in one open room).</p>
    <table><thead><tr><th>ID</th><th>Name</th><th>Capacity</th><th></th></tr></thead>
    <tbody></tbody></table></div>`);
  const tbody = $("tbody", panel);
  for (const r of rows) {
    const tr = el(`<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td>
      <td>${r.capacity}</td><td><button class="small">delete</button></td></tr>`);
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
    if (!id || !name || !(capacity >= 1)) return toast("id, name and capacity ≥ 1 required", true);
    try { await api("POST", "/api/rooms", { id, name, capacity }); render(); }
    catch (e) { toast(e.message, true); }
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
    <div style="overflow-x:auto"><table class="grid-table"><thead>
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
  root.append(panel);
}

// ---------------------------------------------------------------------- CSV

const CSV_ENTITIES = [
  ["students", "id,name"],
  ["teachers", "id,name"],
  ["subjects", "id,name"],
  ["rooms", "id,name,capacity"],
  ["timeslots", "id,date,period,label"],
  ["teacher_subjects", "teacher_id,subject_id"],
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
              <li>🔒 room capacity is never exceeded</li>
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
        <label title="Models the whole problem as a constraint program
          (OR-tools CP-SAT) and optimizes all objectives at once. Usually
          strictly better than the standard solver; falls back to it
          automatically when it cannot do better.">
          <input type="checkbox" id="opt-exact"${state.exact ? " checked" : ""}>
          exact optimizer (CP-SAT)</label>
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
                         teacher_slot_spread: 1, teacher_working_day: 30,
                         teacher_day_spread: 1 };
  let dragKey = null;

  async function putCaps(newCaps) {
    try {
      await api("PUT", "/api/settings", {
        teacher_capacity: settings.teacher_capacity,
        student_day_cap: settings.student_day_cap,
        objective_caps: newCaps,
      });
      render();   // everything revalidates against the new rules
    } catch (e) { toast(e.message, true); render(); }
  }

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
    const li = el(hard
      ? `<li class="prio-item hard-obj" draggable="true" data-key="${key}">
          <span class="prio-rank rank-zero">0</span> ${esc(OBJ_LABELS[key])}
          <span class="hard-bound">≤ <input type="number" min="0" max="999"
            value="${caps[key]}" data-bound="${key}"></span>
          <span class="prio-grip">⠿</span></li>`
      : `<li class="prio-item" draggable="true" data-key="${key}">
          <span class="prio-rank">${rank}</span> ${esc(OBJ_LABELS[key])}
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
      const bound = li.querySelector("input");
      bound.onchange = () => {
        const v = parseInt(bound.value, 10);
        if (!(v >= 0 && v <= 999)) return toast("bound must be 0-999", true);
        putCaps({ ...caps, [key]: v });
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
    btn.textContent = state.exact
      ? `Solving… (~${state.exactBudget}s)` : "Solving…";
    try {
      const res = await api("POST", "/api/schedule/generate", {
        keep_existing: state.keep,
        compress_teacher_days: state.compress,
        solver: state.exact ? "v2" : "v1",
        v2_time_budget: state.exactBudget,
        objective_order: state.objOrder,
      });
      if (res.complete) {
        toast(`Complete schedule: ${res.scheduled} lessons`
          + (state.exact ? ` (${res.backend})` : ""));
      } else toast("Partial schedule — see unscheduled list", true);
      render();
    } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Generate schedule"; }
  };
  $("#opt-keep", ctrl).onchange = (e) => { state.keep = e.target.checked; };
  $("#opt-compress", ctrl).onchange = (e) => { state.compress = e.target.checked; };
  $("#opt-exact", ctrl).onchange = (e) => {
    state.exact = e.target.checked;
    $("#exact-opts", ctrl).hidden = !state.exact;
    $("#opt-compress", ctrl).disabled = state.exact;
    $("#compress-note", ctrl).hidden = !state.exact;
  };
  $("#opt-exact-budget", ctrl).onchange = (e) => {
    const v = parseInt(e.target.value, 10);
    if (v >= 1 && v <= 600) state.exactBudget = v;
  };
  $("#clear", ctrl).onclick = async () => {
    if (!await appConfirm("Delete all scheduled lessons?", "Delete all")) return;
    await api("DELETE", "/api/schedule").catch(e => toast(e.message, true));
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
          `<tr><td>${esc(t.name)}</td><td>${t.lessons}</td>
           <td>${t.days}</td></tr>`).join("")
      }</tbody></table>
      <table><thead><tr><th>Student</th><th>Lessons</th>
        <th>Lesson days</th><th>Two-lesson days</th></tr></thead><tbody>${
        schedule.student_stats.map(s =>
          `<tr${s.double_days.length ? ' class="double-day"' : ""}>
           <td>${esc(s.name)}</td><td>${s.lessons}</td><td>${s.days}</td>
           <td>${s.double_days.length
              ? `${s.double_days.length} (${s.double_days.map(fmtDate).join(", ")})`
              : "—"}</td></tr>`).join("")
      }</tbody></table></div>
      <p class="muted">Student days with two lessons: ${o.student_double_days} ·
        with non-consecutive lessons: ${o.student_day_gaps} ·
        lesson-count spread between teachers (max−min): ${o.slot_spread} ·
        total teacher working days: ${o.total_days} ·
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
  $("#add-lesson", add).onclick = async () => {
    const body = {
      student_id: $("#l-student", add).value,
      subject_id: $("#l-subject", add).value,
      teacher_id: $("#l-teacher", add).value,
      room_id: $("#l-room", add).value,
      timeslot_id: $("#l-slot", add).value,
    };
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
  const badIds = new Set(schedule.violations.flatMap(v => v.lesson_ids));
  const totalLessons = schedule.lessons.length;
  const grid = el(`<div class="panel"><h2>Timetable
    <span class="muted" id="lesson-count">(${totalLessons} lessons)</span></h2>
    <p class="muted">Drag a lesson card onto another timeslot to move it.
      Moves that break a constraint are rejected with an explanation;
      confirm to override.</p></div>`);

  // ---- visibility filter: toggle teachers / students on and off
  const filterActive = state.hiddenTeachers.size || state.hiddenStudents.size;
  const filterBox = el(`<details class="filter-box"${filterActive ? " open" : ""}>
    <summary>Filter <span class="muted" id="filter-note"></span></summary>
    <div class="filter-groups"></div></details>`);
  const groupsEl = $(".filter-groups", filterBox);

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

  function chipGroup(label, people, hiddenSet) {
    const row = el(`<div class="filter-row"><span class="filter-label">
      ${label}</span><span class="filter-chips"></span>
      <span class="filter-quick">
        <button class="small" data-q="all">all</button>
        <button class="small" data-q="none">none</button></span></div>`);
    const chipsEl = $(".filter-chips", row);
    const chips = [];
    for (const p of people) {
      const chip = el(`<button class="chip${hiddenSet.has(p.id) ? " off" : ""}"
        title="click to show/hide">${esc(p.name)}</button>`);
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
  groupsEl.append(chipGroup("Teachers", teachers, state.hiddenTeachers));
  groupsEl.append(chipGroup("Students", students, state.hiddenStudents));
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

    grid.append(calendarTable(overview, (entry) => {
      const box = el(`<div class="cal-entry" data-teacher-id="${entry.teacher_id}">
        <b>${esc(entry.teacher_name)}</b></div>`);
      for (const l of entry.lessons) {
        const card = el(`<div class="lesson-card${badIds.has(l.lesson_id) ? " bad" : ""}"
          draggable="true" data-lesson-id="${l.lesson_id}"
          data-student-id="${l.student_id}">
          ${esc(l.student_name)} — ${esc(l.subject_name)}
          <span class="muted">· ${esc(l.room_name)}</span>
          <button class="edit" title="edit teacher / room / subject">✎</button>
          <button class="del" title="delete">×</button></div>`);
        card.ondragstart = (e) => {
          e.dataTransfer.setData("text/plain", String(l.lesson_id));
          e.dataTransfer.effectAllowed = "move";
        };
        $(".edit", card).onclick = () =>
          openEditor(card, l, entry.teacher_id);
        $(".del", card).onclick = async () => {
          await api("DELETE", `/api/lessons/${l.lesson_id}`).catch(e => toast(e.message, true));
          render();
        };
        box.append(card);
      }
      return box;
    }, dropHook));
    applyFilter();
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
    return el(`<div class="cal-entry"><b>${esc(entry.teacher_name)}</b>${
      entry.lessons.map(l =>
        `<div class="cal-line">${esc(l.student_name)} — ${esc(l.subject_name)}
         <span class="muted">(${esc(l.room_name)})</span></div>`).join("")
    }</div>`);
  }
  if (view === "student") {
    return el(`<div class="cal-entry"><b>${esc(entry.subject_name)}</b>
      <div class="cal-line muted">${esc(entry.teacher_name)} · ${esc(entry.room_name)}</div></div>`);
  }
  return el(`<div class="cal-entry"><b>${esc(entry.subject_name)}</b>
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
  availability: renderAvailability,
  csv: renderCsv,
};

let _lastRenderedTab = null;

async function render() {
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
