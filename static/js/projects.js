// static/js/projects.js
// Projects feature — Phase 1 sidebar section.
// Self-contained: create projects, list them with a member count, expand to see
// member chats, attach existing chats, rename / archive / delete. Membership is
// additive — deleting a project never deletes chats (the backend detaches them).
// See specs/projects-feature-design.md.

const LIST_EL = () => document.getElementById('projects-list');
const expanded = new Set();

function toast(msg) {
  try { if (window.uiModule && window.uiModule.showToast) window.uiModule.showToast(msg); } catch (_) {}
}

async function api(url, { method = 'GET', form = null } = {}) {
  const opts = { method, credentials: 'same-origin' };
  if (form) {
    const fd = new FormData();
    Object.entries(form).forEach(([k, v]) => { if (v !== undefined && v !== null) fd.append(k, v); });
    opts.body = fd;
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

function el(tag, props = {}, children = []) {
  const n = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'style') n.setAttribute('style', v);
    else if (k === 'class') n.className = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null) return;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return n;
}

function refreshSessionSidebar() {
  // Reflect membership/label changes in the Chats list if that module is loaded.
  try {
    const sm = window.sessionModule;
    if (sm && typeof sm.loadSessions === 'function') sm.loadSessions();
  } catch (_) {}
}

async function loadProjects() {
  const host = LIST_EL();
  if (!host) return;
  let projects;
  try {
    projects = await api('/api/projects');
  } catch (e) {
    host.textContent = '';
    host.appendChild(el('div', { style: 'padding:6px 10px;font-size:11px;opacity:0.6;' }, 'Could not load projects'));
    return;
  }
  host.textContent = '';
  if (!projects.length) {
    host.appendChild(el('div', { style: 'padding:6px 10px;font-size:11px;opacity:0.55;' }, 'No projects yet — click + to create one.'));
    return;
  }
  projects.forEach((p) => host.appendChild(renderProjectRow(p)));
}

function renderProjectRow(p) {
  const isOpen = expanded.has(p.id);
  const wrap = el('div', { class: 'project-row', 'data-project-id': p.id, style: 'margin-bottom:2px;' });

  const head = el('div', {
    class: 'list-item',
    style: 'display:flex;align-items:center;gap:6px;cursor:pointer;',
    title: p.goal || p.description || p.name,
    onclick: () => toggleExpand(p.id),
  }, [
    el('span', { style: 'opacity:0.6;font-size:10px;width:10px;display:inline-block;' }, isOpen ? '▾' : '▸'),
    el('span', { style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, p.name),
    el('span', { style: 'opacity:0.55;font-size:10px;' }, String(p.session_count || 0)),
  ]);
  wrap.appendChild(head);

  if (isOpen) wrap.appendChild(renderProjectBody(p));
  return wrap;
}

function renderProjectBody(p) {
  const body = el('div', { class: 'project-body', style: 'margin:2px 0 6px 16px;' });

  const actions = el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;padding:2px 8px 6px;font-size:11px;' });
  const mkBtn = (label, fn) => el('a', {
    href: '#', style: 'opacity:0.7;text-decoration:none;',
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); fn(); },
  }, label);
  actions.appendChild(mkBtn('add chats', () => openAddPicker(p, body)));
  actions.appendChild(mkBtn('rename', () => renameProject(p)));
  actions.appendChild(mkBtn('archive', () => archiveProject(p)));
  actions.appendChild(mkBtn('delete', () => deleteProject(p)));
  body.appendChild(actions);

  const sessWrap = el('div', { class: 'project-sessions', style: 'font-size:12px;' }, 'Loading…');
  body.appendChild(sessWrap);
  loadMemberSessions(p.id, sessWrap);
  return body;
}

async function loadMemberSessions(pid, host) {
  let sessions;
  try { sessions = await api(`/api/project/${pid}/sessions`); }
  catch (_) { host.textContent = 'Could not load chats'; return; }
  host.textContent = '';
  if (!sessions.length) {
    host.appendChild(el('div', { style: 'padding:2px 8px;opacity:0.5;' }, 'No chats yet.'));
    return;
  }
  sessions.forEach((s) => {
    host.appendChild(el('div', {
      class: 'list-item', style: 'display:flex;align-items:center;gap:6px;cursor:pointer;padding-left:8px;',
      title: 'Open chat', onclick: () => { try { window.sessionModule?.selectSession?.(s.id); } catch (_) {} },
    }, [
      el('span', { style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, s.name || 'Untitled'),
      el('a', {
        href: '#', style: 'opacity:0.5;font-size:11px;text-decoration:none;', title: 'Remove from project',
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); detachSession(pid, s.id); },
      }, '✕'),
    ]));
  });
}

async function openAddPicker(p, body) {
  let existing = body.querySelector('.project-add-picker');
  if (existing) { existing.remove(); return; }
  const picker = el('div', { class: 'project-add-picker', style: 'margin:4px 8px;padding:6px;border:1px solid rgba(128,128,128,0.25);border-radius:6px;font-size:12px;' }, 'Loading chats…');
  body.appendChild(picker);
  let sessions;
  try { sessions = await api('/api/sessions'); }
  catch (_) { picker.textContent = 'Could not load chats'; return; }
  const candidates = sessions.filter((s) => s.project_id !== p.id);
  picker.textContent = '';
  picker.appendChild(el('div', { style: 'opacity:0.6;margin-bottom:4px;' }, candidates.length ? 'Add a chat:' : 'No other chats to add.'));
  candidates.slice(0, 100).forEach((s) => {
    picker.appendChild(el('div', {
      class: 'list-item', style: 'cursor:pointer;display:flex;gap:6px;',
      title: s.project_id ? 'Currently in another project — will be moved' : 'Add to project',
      onclick: () => attachSession(p.id, s.id),
    }, [
      el('span', { style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, s.name || 'Untitled'),
      s.project_id ? el('span', { style: 'opacity:0.45;font-size:10px;' }, 'move') : null,
    ]));
  });
}

async function attachSession(pid, sid) {
  try { await api(`/api/project/${pid}/session/${sid}`, { method: 'POST' }); toast('Added to project'); }
  catch (e) { toast('Add failed: ' + e.message); return; }
  await loadProjects(); refreshSessionSidebar();
}

async function detachSession(pid, sid) {
  try { await api(`/api/project/${pid}/session/${sid}`, { method: 'DELETE' }); toast('Removed from project'); }
  catch (e) { toast('Remove failed: ' + e.message); return; }
  await loadProjects(); refreshSessionSidebar();
}

async function createProject() {
  const name = (window.prompt('New project name:') || '').trim();
  if (!name) return;
  const goal = (window.prompt('Project goal (optional — shared context for its chats):') || '').trim();
  try { await api('/api/project', { method: 'POST', form: { name, goal } }); toast('Project created'); }
  catch (e) { toast('Create failed: ' + e.message); return; }
  await loadProjects();
}

async function renameProject(p) {
  const name = (window.prompt('Rename project:', p.name) || '').trim();
  if (!name || name === p.name) return;
  try { await api(`/api/project/${p.id}`, { method: 'PATCH', form: { name } }); }
  catch (e) { toast('Rename failed: ' + e.message); return; }
  await loadProjects();
}

async function archiveProject(p) {
  if (!window.confirm(`Archive "${p.name}"? Its chats stay in your Chats list. Archived projects are permanently deleted after 30 days.`)) return;
  try { await api(`/api/project/${p.id}/archive`, { method: 'POST' }); toast('Project archived'); }
  catch (e) { toast('Archive failed: ' + e.message); return; }
  expanded.delete(p.id);
  await loadProjects();
}

async function deleteProject(p) {
  if (!window.confirm(`Delete "${p.name}" now? Its chats are NOT deleted — they stay in your Chats list, just ungrouped.`)) return;
  try { await api(`/api/project/${p.id}`, { method: 'DELETE' }); toast('Project deleted'); }
  catch (e) { toast('Delete failed: ' + e.message); return; }
  expanded.delete(p.id);
  await loadProjects(); refreshSessionSidebar();
}

function toggleExpand(pid) {
  if (expanded.has(pid)) expanded.delete(pid); else expanded.add(pid);
  loadProjects();
}

function init() {
  const btn = document.getElementById('project-new-btn');
  if (btn && !btn._projBound) { btn._projBound = true; btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); createProject(); }); }
  loadProjects();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

const projectsModule = { init, reload: loadProjects };
window.projectsModule = projectsModule;
export default projectsModule;
