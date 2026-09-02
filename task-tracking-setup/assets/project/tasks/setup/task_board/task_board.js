(function () {
  "use strict";

  const STATUS_ORDER = ["backlog", "ready", "in_progress", "blocked", "done"];
  const STATUS_NAMES = {
    backlog: "Backlog",
    ready: "Ready",
    in_progress: "In progress",
    blocked: "Blocked",
    done: "Done",
  };

  function element(id) {
    const found = document.getElementById(id);
    if (!found) throw new Error(`Required board element is missing: #${id}`);
    return found;
  }

  function showError(message, detail) {
    const panel = document.getElementById("boardError");
    if (!panel) {
      document.body.textContent = `Task board unavailable: ${message}`;
      return;
    }
    const messageNode = document.getElementById("boardErrorMessage");
    const detailNode = document.getElementById("boardErrorDetail");
    if (messageNode) messageNode.textContent = message;
    if (detailNode) detailNode.textContent = detail || "";
    panel.hidden = false;
    const application = document.getElementById("boardApplication");
    if (application) application.hidden = true;
  }

  function requireString(value, label) {
    if (typeof value !== "string") throw new Error(`${label} must be a string`);
    return value;
  }

  function requireNonEmptyString(value, label) {
    requireString(value, label);
    if (!value.trim()) throw new Error(`${label} must be non-empty`);
    return value;
  }

  function validateTask(task, index, collection) {
    if (!task || typeof task !== "object" || Array.isArray(task)) {
      throw new Error(`${collection}[${index}] must be an object`);
    }
    ["id", "type", "title", "owner", "section", "status", "priority", "urgency"].forEach((field) => {
      requireNonEmptyString(task[field], `${collection}[${index}].${field}`);
    });
    if (!STATUS_ORDER.includes(task.status)) {
      throw new Error(`${collection}[${index}].status is incompatible`);
    }
    if (!Array.isArray(task.tags) || task.tags.some((tag) => typeof tag !== "string")) {
      throw new Error(`${collection}[${index}].tags must contain strings`);
    }
    if (!Array.isArray(task.planning_doc_links)) {
      throw new Error(`${collection}[${index}].planning_doc_links must be an array`);
    }
    task.planning_doc_links.forEach((link, linkIndex) => {
      if (!link || typeof link !== "object") {
        throw new Error(`${collection}[${index}].planning_doc_links[${linkIndex}] is invalid`);
      }
      const href = requireNonEmptyString(link.href, `${collection}[${index}].planning_doc_links[${linkIndex}].href`);
      requireNonEmptyString(link.label, `${collection}[${index}].planning_doc_links[${linkIndex}].label`);
      if (!href.startsWith("../ideation/")) {
        throw new Error(`${collection}[${index}].planning_doc_links[${linkIndex}].href is unsafe`);
      }
    });
    return task;
  }

  function validatePayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Task board data payload is missing or corrupt");
    }
    if (payload.board_data_version !== 1) {
      throw new Error(`Unsupported board_data_version: ${String(payload.board_data_version)}`);
    }
    if (payload.tracker_schema_version !== 4) {
      throw new Error(`Unsupported tracker_schema_version: ${String(payload.tracker_schema_version)}`);
    }
    if (!payload.config || typeof payload.config !== "object") {
      throw new Error("Task board config is missing");
    }
    requireString(payload.config.project, "config.project");
    if (!Array.isArray(payload.active_tasks) || !Array.isArray(payload.archived_tasks)) {
      throw new Error("Task board active_tasks and archived_tasks must be arrays");
    }
    return {
      activeTasks: payload.active_tasks.map((task, index) => validateTask(task, index, "active_tasks")),
      archivedTasks: payload.archived_tasks.map((task, index) => validateTask(task, index, "archived_tasks")),
      config: payload.config,
      sources: Array.isArray(payload.sources) ? payload.sources : [],
    };
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function detailItem(label, value) {
    if (!value || (Array.isArray(value) && !value.length)) return "";
    const rendered = Array.isArray(value) ? value.join(", ") : value;
    return `<div class="detail-item"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(rendered)}</dd></div>`;
  }

  function planningLinks(task) {
    if (!task.planning_doc_links.length) return "";
    const links = task.planning_doc_links.map((link) => (
      `<a href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)}</a>`
    )).join("");
    return `<div class="planning-links">${links}</div>`;
  }

  function tags(task) {
    return task.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  }

  function taskDetail(task) {
    const reproduction = [
      detailItem("Reproduction", task.reproduction),
      detailItem("Expected", task.expected),
      detailItem("Actual", task.actual),
    ].join("");
    return `<dl class="task-detail">
      ${detailItem("Description", task.description)}
      ${detailItem("Acceptance", task.acceptance)}
      ${detailItem("Blocker / notes", task.notes)}
      ${detailItem("Parent", task.parent_id)}
      ${detailItem("Dependencies", task.depends_on)}
      ${reproduction}
      ${planningLinks(task)}
    </dl>`;
  }

  function card(task, archived) {
    return `<article class="task-card"><details>
      <summary class="card-summary">
        <div class="card-top"><span class="task-id">${escapeHtml(task.id)}</span><span class="pill">${escapeHtml(task.type)}</span>${archived ? '<span class="archived-badge">Archived</span>' : ""}<span class="priority ${escapeHtml(task.priority)}">${escapeHtml(task.priority)}</span></div>
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="task-subtitle">${escapeHtml(task.section)} · ${escapeHtml(task.owner)}</div>
        <div class="tag-list">${tags(task)}</div>
      </summary>
      ${taskDetail(task)}
    </details></article>`;
  }

  function listRow(task, archived) {
    return `<details class="task-row"><summary class="task-row-summary">
      <span class="task-id">${escapeHtml(task.id)}</span>
      <span class="row-title">${escapeHtml(task.title)}</span>
      <span class="status ${escapeHtml(task.status)}">${escapeHtml(STATUS_NAMES[task.status])}</span>
      <span class="${escapeHtml(task.priority)}">${escapeHtml(task.priority)}</span>
      <span>${escapeHtml(task.owner)}</span>
      <span>${archived ? '<span class="archived-badge">Archived</span>' : escapeHtml(task.section)}</span>
    </summary>${taskDetail(task)}</details>`;
  }

  function initializeBoard(boardData) {
    const application = element("boardApplication");
    const errorPanel = element("boardError");
    const kanbanView = element("kanbanView");
    const listView = element("listView");
    const kanbanButton = element("kanbanViewButton");
    const listButton = element("listViewButton");
    const taskSearch = element("taskSearch");
    const showArchived = element("showArchived");
    const clearFilters = element("clearFiltersButton");
    const activeFilterCount = element("activeFilterCount");
    const resultSummary = element("resultSummary");
    const labelFilters = element("labelFilters");
    const tagSearch = element("tagSearch");
    const tagSuggestions = element("tagSuggestions");
    const tagFilters = element("tagFilters");
    const sectionFilters = element("sectionFilters");
    const sectionFilterCount = element("sectionFilterCount");

    const activeTasks = boardData.activeTasks.map((task) => ({ task, archived: false }));
    const archivedTasks = boardData.archivedTasks.map((task) => ({ task, archived: true }));
    const allTasks = [...activeTasks, ...archivedTasks];
    const sections = [...new Set(allTasks.map(({ task }) => task.section || "Unsectioned"))].sort((a, b) => a.localeCompare(b));
    const hashtags = [...new Set(allTasks.flatMap(({ task }) => task.tags))].sort((a, b) => a.localeCompare(b));
    const selected = { labels: new Set(), tags: new Set(), sections: new Set() };
    let suggestionIndex = -1;

    element("projectName").textContent = boardData.config.project;
    const archiveCount = boardData.archivedTasks.length;
    const sourceCount = boardData.sources.length;
    element("boardMeta").textContent = `${boardData.activeTasks.length} active · ${archiveCount} archived · ${sourceCount} sources`;
    showArchived.checked = Boolean(boardData.config.show_archived);

    function searchKey(value) {
      return String(value ?? "").trim().toLocaleLowerCase();
    }

    function filterButton(group, value, label) {
      const pressed = selected[group].has(value);
      return `<button type="button" class="filter-chip" data-filter-group="${escapeHtml(group)}" data-filter-value="${escapeHtml(value)}" aria-pressed="${pressed}">${escapeHtml(label)}</button>`;
    }

    function activeCount() {
      return selected.labels.size + selected.tags.size + selected.sections.size + (taskSearch.value.trim() ? 1 : 0) + (showArchived.checked ? 1 : 0);
    }

    function matchingHashtags(query) {
      const key = searchKey(query).replace(/^#+/, "");
      if (!key) return [];
      return hashtags.filter((tag) => !selected.tags.has(tag) && searchKey(tag).replace(/^#+/, "").includes(key)).slice(0, 8);
    }

    function renderTagSuggestions() {
      const matches = matchingHashtags(tagSearch.value);
      suggestionIndex = Math.min(suggestionIndex, matches.length - 1);
      if (!matches.length) {
        tagSuggestions.innerHTML = tagSearch.value.trim() ? '<div class="tag-suggestion-empty">No matching hashtags</div>' : "";
        tagSuggestions.hidden = !tagSearch.value.trim();
        tagSearch.setAttribute("aria-expanded", String(!tagSuggestions.hidden));
        tagSearch.removeAttribute("aria-activedescendant");
        return;
      }
      tagSuggestions.innerHTML = matches.map((tag, index) => (
        `<button id="tagSuggestion${index}" type="button" class="tag-suggestion${index === suggestionIndex ? " active" : ""}" data-tag-suggestion="${escapeHtml(tag)}" role="option" aria-selected="${index === suggestionIndex}" tabindex="-1"><span>${escapeHtml(tag)}</span></button>`
      )).join("");
      tagSuggestions.hidden = false;
      tagSearch.setAttribute("aria-expanded", "true");
      if (suggestionIndex >= 0) tagSearch.setAttribute("aria-activedescendant", `tagSuggestion${suggestionIndex}`);
    }

    function selectHashtag(tag) {
      if (!hashtags.includes(tag)) return;
      selected.tags.add(tag);
      tagSearch.value = "";
      suggestionIndex = -1;
      render();
      tagSearch.focus();
    }

    function visibleTasks() {
      const selectedStatuses = [...selected.labels].filter((value) => value.startsWith("status:")).map((value) => value.slice(7));
      const selectedUrgencies = [...selected.labels].filter((value) => value.startsWith("urgency:")).map((value) => value.slice(8));
      const query = searchKey(taskSearch.value);
      return allTasks.filter(({ task, archived }) => {
        if (archived && !showArchived.checked) return false;
        if (selectedStatuses.length && !selectedStatuses.includes(task.status)) return false;
        if (selectedUrgencies.length && !selectedUrgencies.includes(task.urgency)) return false;
        if (selected.tags.size && !task.tags.some((tag) => selected.tags.has(tag))) return false;
        if (selected.sections.size && !selected.sections.has(task.section || "Unsectioned")) return false;
        if (!query) return true;
        const searchable = [task.id, task.title, task.owner, task.section, ...task.tags].map(searchKey).join(" ");
        return searchable.includes(query);
      });
    }

    function renderFilters() {
      const taskPool = showArchived.checked ? allTasks : activeTasks;
      const labels = [
        ...STATUS_ORDER.map((status) => ({ key: `status:${status}`, label: `${STATUS_NAMES[status]} · ${taskPool.filter(({ task }) => task.status === status).length}` })),
        { key: "urgency:urgent", label: `Urgent · ${taskPool.filter(({ task }) => task.urgency === "urgent").length}` },
        { key: "urgency:high", label: `High urgency · ${taskPool.filter(({ task }) => task.urgency === "high").length}` },
      ];
      labelFilters.innerHTML = labels.map((item) => filterButton("labels", item.key, item.label)).join("");
      tagFilters.innerHTML = [...selected.tags].sort().map((tag) => filterButton("tags", tag, tag)).join("");
      sectionFilters.innerHTML = sections.map((section) => filterButton("sections", section, section)).join("") || '<span class="meta">No sections</span>';
      sectionFilterCount.textContent = selected.sections.size ? `· ${selected.sections.size}` : "";
      const count = activeCount();
      activeFilterCount.textContent = String(count);
      clearFilters.disabled = count === 0;
      renderTagSuggestions();
    }

    function renderViews() {
      const visible = visibleTasks();
      kanbanView.innerHTML = STATUS_ORDER.map((status) => {
        const items = visible.filter(({ task }) => task.status === status);
        return `<section class="column" aria-labelledby="column-${status}"><div class="column-heading"><span id="column-${status}">${STATUS_NAMES[status]}</span><span class="meta">${items.length}</span></div>${items.map(({ task, archived }) => card(task, archived)).join("") || '<div class="empty">No tasks</div>'}</section>`;
      }).join("");
      listView.innerHTML = sections.map((section) => {
        const items = visible.filter(({ task }) => (task.section || "Unsectioned") === section);
        if (!items.length) return "";
        return `<details class="list-group" open><summary>${escapeHtml(section)}<span class="list-group-count">${items.length}</span></summary>${items.map(({ task, archived }) => listRow(task, archived)).join("")}</details>`;
      }).join("") || '<div class="empty">No matching tasks</div>';
      resultSummary.textContent = `${visible.length} task${visible.length === 1 ? "" : "s"} shown`;
    }

    function render() {
      renderFilters();
      renderViews();
    }

    function setView(view, moveFocus = false) {
      const showKanban = view === "kanban";
      kanbanView.hidden = !showKanban;
      listView.hidden = showKanban;
      kanbanButton.setAttribute("aria-selected", String(showKanban));
      listButton.setAttribute("aria-selected", String(!showKanban));
      kanbanButton.tabIndex = showKanban ? 0 : -1;
      listButton.tabIndex = showKanban ? -1 : 0;
      if (moveFocus) (showKanban ? kanbanButton : listButton).focus();
    }

    element("filterHeading").closest(".controls").addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter-group]");
      if (!button) return;
      const group = button.dataset.filterGroup;
      const value = button.dataset.filterValue;
      if (!selected[group]) throw new Error(`Unknown filter group: ${group}`);
      selected[group].has(value) ? selected[group].delete(value) : selected[group].add(value);
      render();
    });
    taskSearch.addEventListener("input", render);
    showArchived.addEventListener("change", render);
    clearFilters.addEventListener("click", () => {
      selected.labels.clear();
      selected.tags.clear();
      selected.sections.clear();
      taskSearch.value = "";
      showArchived.checked = false;
      render();
      taskSearch.focus();
    });
    tagSearch.addEventListener("input", () => { suggestionIndex = -1; renderTagSuggestions(); });
    tagSearch.addEventListener("focus", renderTagSuggestions);
    tagSearch.addEventListener("keydown", (event) => {
      const matches = matchingHashtags(tagSearch.value);
      if (event.key === "Escape") {
        tagSearch.value = "";
        suggestionIndex = -1;
        renderTagSuggestions();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!matches.length) return;
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        suggestionIndex = (suggestionIndex + direction + matches.length) % matches.length;
        renderTagSuggestions();
        return;
      }
      if (event.key !== "Enter" || !matches.length) return;
      event.preventDefault();
      selectHashtag(matches[suggestionIndex >= 0 ? suggestionIndex : 0]);
    });
    tagSuggestions.addEventListener("click", (event) => {
      const button = event.target.closest("[data-tag-suggestion]");
      if (button) selectHashtag(button.dataset.tagSuggestion);
    });
    document.addEventListener("click", (event) => {
      if (event.target.closest(".tag-search-wrap")) return;
      tagSuggestions.hidden = true;
      tagSearch.setAttribute("aria-expanded", "false");
      tagSearch.removeAttribute("aria-activedescendant");
    });
    kanbanButton.addEventListener("click", () => setView("kanban"));
    listButton.addEventListener("click", () => setView("list"));
    [kanbanButton, listButton].forEach((button, index, buttons) => {
      button.addEventListener("keydown", (event) => {
        const destinationByKey = {
          ArrowLeft: (index - 1 + buttons.length) % buttons.length,
          ArrowRight: (index + 1) % buttons.length,
          Home: 0,
          End: buttons.length - 1,
        };
        const next = destinationByKey[event.key];
        if (next === undefined) return;
        event.preventDefault();
        setView(next === 0 ? "kanban" : "list", true);
      });
    });

    errorPanel.hidden = true;
    application.hidden = false;
    setView(boardData.config.default_view === "list" ? "list" : "kanban");
    render();
  }

  try {
    initializeBoard(validatePayload(globalThis.__TASK_BOARD_DATA__));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showError("The task board could not initialize.", message);
    if (globalThis.console && typeof globalThis.console.error === "function") {
      globalThis.console.error("Task board initialization failed", error);
    }
  }
}());
