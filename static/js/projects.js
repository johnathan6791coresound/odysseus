// static/js/projects.js
// Projects feature — Phase 1 sidebar section.
// Create projects, list them with a member count, expand to see member chats,
// attach existing chats, rename / archive / delete. Membership is additive —
// deleting a project never deletes chats (the backend detaches them).
//
// Styling: all visuals come from theme-driven CSS classes in style.css
// (.project-*, .list-item, .grow) so it tracks every preset + custom theme.
// Dialogs use the app's themed uiModule.styledPrompt/styledConfirm — never
// native prompt()/confirm() or bare <a> links. See specs/projects-feature-design.md.

import uiModule from './ui.js';

const LIST_EL = () => document.getElementById('projects-list');
const expanded = new Set();

function toast(msg) { try { uiModule.showToast(msg); } catch (_) {} }

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
    if (v === null || v === undefined) return;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null) return;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return n;
}

function refreshSessionSidebar() {
  try {
    const sm = window.sessionModule;
    if (sm && typeof sm.loadSessions === 'function') sm.loadSessions();
  } catch (_) {}
}

async function loadProjects() {
  const host = LIST_EL();
  if (!host) return;
  let projects;
  try { projects = await api('/api/projects'); }
  catch (_) {
    host.textContent = '';
    host.appendChild(el('div', { class: 'project-empty' }, 'Could not load projects'));
    return;
  }
  host.textContent = '';
  if (!projects.length) {
    host.appendChild(el('div', { class: 'project-empty' }, 'No projects yet — click + to create one.'));
    return;
  }
  projects.forEach((p) => host.appendChild(renderProjectRow(p)));
}

function renderProjectRow(p) {
  const isOpen = expanded.has(p.id);
  const wrap = el('div', { class: 'project-row', 'data-project-id': p.id });
  wrap.appendChild(el('div', {
    class: 'list-item project-head',
    title: p.goal || p.description || p.name,
    onclick: () => toggleExpand(p.id),
  }, [
    el('span', { class: 'project-caret' }, isOpen ? '▾' : '▸'),
    el('span', { class: 'project-name grow' }, p.name),
    el('span', { class: 'project-count' }, String(p.session_count || 0)),
  ]));
  if (isOpen) wrap.appendChild(renderProjectBody(p));
  return wrap;
}

function renderProjectBody(p) {
  const body = el('div', { class: 'project-body' });
  const actions = el('div', { class: 'project-actions' });
  const mkBtn = (label, fn, danger = false) => el('button', {
    type: 'button', class: 'project-action' + (danger ? ' danger' : ''),
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); fn(); },
  }, label);
  actions.appendChild(mkBtn('add chats', () => openAddPicker(p, body)));
  actions.appendChild(mkBtn('rename', () => renameProject(p)));
  actions.appendChild(mkBtn('archive', () => archiveProject(p)));
  actions.appendChild(mkBtn('delete', () => deleteProject(p), true));
  body.appendChild(actions);

  const sessWrap = el('div', { class: 'project-sessions' }, el('div', { class: 'project-empty' }, 'Loading…'));
  body.appendChild(sessWrap);
  loadMemberSessions(p.id, sessWrap);
  return body;
}

async function loadMemberSessions(pid, host) {
  let sessions;
  try { sessions = await api(`/api/project/${pid}/sessions`); }
  catch (_) { host.textContent = ''; host.appendChild(el('div', { class: 'project-empty' }, 'Could not load chats')); return; }
  host.textContent = '';
  if (!sessions.length) {
    host.appendChild(el('div', { class: 'project-empty' }, 'No chats yet.'));
    return;
  }
  sessions.forEach((s) => {
    host.appendChild(el('div', {
      class: 'list-item project-member',
      title: 'Open chat',
      onclick: () => { try { window.sessionModule?.selectSession?.(s.id); } catch (_) {} },
    }, [
      el('span', { class: 'project-name grow' }, s.name || 'Untitled'),
      el('button', {
        type: 'button', class: 'project-remove', title: 'Remove from project', 'aria-label': 'Remove from project',
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); detachSession(pid, s.id); },
      }, '✕'),
    ]));
  });
}

async function openAddPicker(p, body) {
  const existing = body.querySelector('.project-picker');
  if (existing) { existing.remove(); return; }
  const picker = el('div', { class: 'project-picker' }, el('div', { class: 'project-empty' }, 'Loading chats…'));
  body.appendChild(picker);
  let sessions;
  try { sessions = await api('/api/sessions'); }
  catch (_) { picker.textContent = ''; picker.appendChild(el('div', { class: 'project-empty' }, 'Could not load chats')); return; }
  const candidates = sessions.filter((s) => s.project_id !== p.id);
  picker.textContent = '';
  picker.appendChild(el('div', { class: 'project-picker-title' }, candidates.length ? 'Add a chat:' : 'No other chats to add.'));
  candidates.slice(0, 100).forEach((s) => {
    picker.appendChild(el('div', {
      class: 'list-item',
      title: s.project_id ? 'Currently in another project — will be moved' : 'Add to project',
      onclick: () => attachSession(p.id, s.id),
    }, [
      el('span', { class: 'project-name grow' }, s.name || 'Untitled'),
      s.project_id ? el('span', { class: 'project-move-tag' }, 'move') : null,
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
  const name = await uiModule.styledPrompt('', { title: 'New project', placeholder: 'Project name', confirmText: 'Create', maxLength: 80 });
  if (!name) return;
  const goal = await uiModule.styledPrompt('A shared goal injected into every chat in this project.', {
    title: 'Project goal (optional)', placeholder: 'Goal', confirmText: 'Save', cancelText: 'Skip', maxLength: 400,
  });
  try { await api('/api/project', { method: 'POST', form: { name, goal: goal || '' } }); toast('Project created'); }
  catch (e) { toast('Create failed: ' + e.message); return; }
  await loadProjects();
}

async function renameProject(p) {
  const name = await uiModule.styledPrompt('', { title: 'Rename project', defaultValue: p.name, confirmText: 'Save', maxLength: 80 });
  if (!name || name === p.name) return;
  try { await api(`/api/project/${p.id}`, { method: 'PATCH', form: { name } }); }
  catch (e) { toast('Rename failed: ' + e.message); return; }
  await loadProjects();
}

async function archiveProject(p) {
  const ok = await uiModule.styledConfirm(
    `Archive "${p.name}"? Its chats stay in your Chats list. Archived projects are permanently deleted after 30 days.`,
    { confirmText: 'Archive', cancelText: 'Cancel' });
  if (!ok) return;
  try { await api(`/api/project/${p.id}/archive`, { method: 'POST' }); toast('Project archived'); }
  catch (e) { toast('Archive failed: ' + e.message); return; }
  expanded.delete(p.id);
  await loadProjects();
}

async function deleteProject(p) {
  const ok = await uiModule.styledConfirm(
    `Delete "${p.name}" now? Its chats are NOT deleted — they stay in your Chats list, just ungrouped.`,
    { confirmText: 'Delete', cancelText: 'Cancel', danger: true });
  if (!ok) return;
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
  if (btn && !btn._projBound) {
    btn._projBound = true;
    btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); createProject(); });
  }
  loadProjects();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

const projectsModule = { init, reload: loadProjects };
window.projectsModule = projectsModule;
export default projectsModule;
