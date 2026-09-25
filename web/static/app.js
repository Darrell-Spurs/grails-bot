/* Grails-Bot Admin - progressive enhancement.

   Everything here is additive. With JavaScript off the panel still renders,
   navigates, and submits every form: the workbench falls back to every album
   stacked (see the <noscript> block in base.html), rarity saves through a
   normal POST with the <noscript> Save button, and destructive actions post
   directly -- the server still requires its own confirm=yes field. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- Theme ------------------------------------------------------------- */
  // The initial value is applied by an inline script in base.html so the page
  // never paints the wrong theme; this only handles later clicks.
  function currentTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("gb-theme", theme); } catch (e) { /* private mode */ }

    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    var dark = theme === "dark";
    // Both glyphs ship in the markup; we swap which one is visible so there is
    // no icon-shaped gap while the other renders.
    var moon = btn.querySelector('[data-theme-icon="dark"]');
    var sun = btn.querySelector('[data-theme-icon="light"]');
    if (moon) moon.hidden = dark;
    if (sun) sun.hidden = !dark;
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    btn.setAttribute("title", btn.getAttribute("aria-label"));
  }

  var themeBtn = document.querySelector("[data-theme-toggle]");
  if (themeBtn) {
    applyTheme(currentTheme());
    themeBtn.addEventListener("click", function () {
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
    });
  }

  /* ---- Mobile navigation ------------------------------------------------- */
  var navToggle = document.querySelector("[data-nav-toggle]");
  var navLinks = document.getElementById("nav-links");
  if (navToggle && navLinks) {
    navToggle.addEventListener("click", function () {
      var open = navLinks.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      navToggle.setAttribute("aria-label", open ? "Hide navigation" : "Show navigation");
    });
  }

  /* ---- Flash toasts ------------------------------------------------------ */
  function dismissFlash(flash) {
    flash.classList.add("leaving");
    setTimeout(function () { flash.remove(); }, reduceMotion ? 0 : 180);
  }

  document.querySelectorAll(".flash").forEach(function (flash) {
    var close = flash.querySelector(".flash-close");
    if (close) close.addEventListener("click", function () { dismissFlash(flash); });
    // Errors stay until dismissed; successes clear themselves.
    if (flash.classList.contains("flash-success")) {
      setTimeout(function () { dismissFlash(flash); }, 5000);
    }
  });

  /* ---- Confirmation dialog ----------------------------------------------- */
  // Replaces window.confirm(): named scope, an explicit destructive verb, and
  // a dialog that stays open (with an inline error) if the request fails.
  var dialog = document.getElementById("confirm-dialog");

  function setupConfirm() {
    if (!dialog || typeof dialog.showModal !== "function") return false;

    var titleEl = dialog.querySelector(".confirm-title");
    var bodyEl = dialog.querySelector(".confirm-body");
    var errorEl = dialog.querySelector(".confirm-error");
    var cancelBtn = dialog.querySelector("[data-confirm-cancel]");
    var acceptBtn = dialog.querySelector("[data-confirm-accept]");
    var pendingForm = null;
    var opener = null;

    function close() {
      dialog.close();
      pendingForm = null;
      acceptBtn.disabled = false;
      acceptBtn.removeAttribute("data-busy");
      // Focus goes back to the control that opened the dialog.
      if (opener && document.contains(opener)) opener.focus();
      opener = null;
    }

    cancelBtn.addEventListener("click", close);
    // Esc closes via the dialog's own cancel event; keep our state in step.
    dialog.addEventListener("cancel", function (e) { e.preventDefault(); close(); });

    acceptBtn.addEventListener("click", function () {
      if (!pendingForm) return;
      errorEl.hidden = true;
      acceptBtn.disabled = true;
      acceptBtn.setAttribute("data-busy", "1");

      // Submitting navigates away on success. If the request fails outright the
      // browser shows its own error page, so the recoverable case we can handle
      // here is a form that refuses to submit at all.
      try {
        pendingForm.submit();
      } catch (err) {
        acceptBtn.disabled = false;
        acceptBtn.removeAttribute("data-busy");
        errorEl.textContent = "Could not submit the request: " + err.message;
        errorEl.hidden = false;
      }
    });

    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (pendingForm === form) return;   // already confirmed; let it through
        e.preventDefault();

        pendingForm = form;
        opener = document.activeElement;
        titleEl.textContent = form.getAttribute("data-confirm-title") || "Are you sure?";
        bodyEl.textContent = form.getAttribute("data-confirm-body") || "";
        acceptBtn.textContent = form.getAttribute("data-confirm-verb") || "Confirm";
        errorEl.hidden = true;

        dialog.showModal();
        // Irreversible: Cancel takes focus, not the destructive button.
        cancelBtn.focus();
      });
    });
    return true;
  }

  if (!setupConfirm()) {
    // <dialog> unsupported: fall back to the native prompt rather than letting
    // destructive forms submit unchallenged.
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        var msg = (form.getAttribute("data-confirm-title") || "Are you sure?") +
                  "\n\n" + (form.getAttribute("data-confirm-body") || "");
        if (!window.confirm(msg)) { e.preventDefault(); e.stopImmediatePropagation(); }
      });
    });
  }

  /* ---- Discography workbench --------------------------------------------- */
  // Left pane is a tablist; picking an album swaps the right pane in place.
  document.querySelectorAll('[role="tablist"].wb-albums').forEach(function (list) {
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;

    function select(tab, moveFocus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      if (moveFocus) tab.focus();
      // Keep the chosen album visible in a scrolled pane without yanking the page.
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(tab, false); });
      tab.addEventListener("keydown", function (e) {
        // The pane is vertical on desktop and horizontal on narrow screens, so
        // both axes move between albums.
        var next = null;
        if (e.key === "ArrowDown" || e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
        else if (e.key === "ArrowUp" || e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") next = tabs[0];
        else if (e.key === "End") next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); select(next, true); }
      });
    });
  });

  /* ---- Rarity autosave --------------------------------------------------- */
  // Saves on change and reports in the cell itself: a drawn-on checkmark plus a
  // revert control, both of which clear themselves after a few seconds. The
  // <select> is disabled in flight, which is what stops a duplicate request.

  function svg(paths, cls) {
    return '<svg class="icon ' + (cls || "") + '" width="15" height="15" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";
  }

  var TICK = svg('<path d="M20 6 9 17l-5-5"/>');
  var UNDO = svg('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>');
  var SPIN = svg('<path d="M21 12a9 9 0 1 1-6.219-8.56"/>', "spin");

  var FEEDBACK_MS = 6000;

  function saveRarity(select, isRevert) {
    var form = select.form;
    var cell = form.querySelector("[data-save-feedback]");
    var value = select.value;
    var previous = select.getAttribute("data-rarity");

    // Any pending clear from an earlier save on this row is now stale.
    if (cell && cell._timer) { clearTimeout(cell._timer); cell._timer = null; }

    function show(html, state) {
      if (!cell) return;
      cell.classList.remove("leaving");
      cell.innerHTML = html;
      cell.setAttribute("data-state", state);
    }

    function clearLater() {
      if (!cell) return;
      cell._timer = setTimeout(function () {
        cell.classList.add("leaving");
        cell._timer = setTimeout(function () {
          cell.innerHTML = "";
          cell.removeAttribute("data-state");
          cell.classList.remove("leaving");
        }, 300);
      }, FEEDBACK_MS);
    }

    // Snapshot the payload BEFORE disabling: a disabled control is omitted from
    // FormData, which would post the form without its rarity field.
    var payload = new FormData(form);

    select.setAttribute("data-rarity", value);   // recolour to the pending value
    select.disabled = true;
    show('<span class="save-spinner">' + SPIN + "</span>", "saving");

    fetch(form.action, {
      method: "POST",
      headers: { "Accept": "application/json" },
      body: payload,
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.json().catch(function () {
          throw new Error("Unexpected response (HTTP " + res.status + ")");
        }).then(function (data) {
          if (!res.ok || !data.ok) throw new Error(data.error || "HTTP " + res.status);
          return data;
        });
      })
      .then(function () {
        var row = select.closest("tr");
        if (row) {
          document.dispatchEvent(new CustomEvent("gb:rarity-saved", {
            detail: { row: row, rarity: value },
          }));
        }
        // Reverting a revert would be a loop with no clear end, so the undo
        // affordance is only offered on a forward change.
        var html = '<span class="save-tick">' + TICK + "</span>";
        if (!isRevert) {
          html += '<button type="button" class="save-revert" data-revert ' +
                  'title="Revert to ' + previous + '" ' +
                  'aria-label="Revert to ' + previous + '">' + UNDO + "</button>";
        }
        show(html, "saved");

        var undo = cell && cell.querySelector("[data-revert]");
        if (undo) {
          undo.addEventListener("click", function () {
            select.value = previous;
            saveRarity(select, true);
          });
        }
        clearLater();
      })
      .catch(function (err) {
        // Roll the control back so what is shown matches what is stored.
        select.value = previous;
        select.setAttribute("data-rarity", previous);
        show('<span class="save-error">' + (err.message || "Save failed.") +
             ' <button type="button" class="retry">Retry</button></span>', "error");
        var retry = cell && cell.querySelector(".retry");
        if (retry) {
          retry.addEventListener("click", function () {
            select.value = value;
            saveRarity(select, isRevert);
          });
        }
        // An error stays until it is dealt with; it is not cleared on a timer.
      })
      .finally(function () {
        select.disabled = false;
        // Selection, scroll and focus all survive: nothing was re-rendered, and
        // the control the admin was using is the one that regains focus.
        if (document.activeElement === document.body) select.focus();
      });
  }

  document.querySelectorAll("select[data-rarity-select]").forEach(function (select) {
    select.addEventListener("change", function () { saveRarity(select, false); });
  });

  /* ---- Workbench filters (release type + rarity) -------------------------- */
  // Both filters narrow the same album pane, so they are applied together from
  // one place. Rarity also hides non-matching track rows inside every panel.
  (function () {
    var pane = document.querySelector(".wb-albums");
    if (!pane) return;

    var typeButtons = document.querySelectorAll("[data-album-filter]");
    var raritySelect = document.querySelector("[data-rarity-filter]");
    if (!typeButtons.length && !raritySelect) return;

    var note = pane.querySelector(".wb-empty");
    var tabs = Array.prototype.slice.call(pane.querySelectorAll(".wb-album"));

    function currentType() {
      var pressed = document.querySelector('[data-album-filter][aria-pressed="true"]');
      return pressed ? pressed.getAttribute("data-album-filter") : "all";
    }
    function currentRarity() {
      return raritySelect ? raritySelect.value : "all";
    }

    function apply() {
      var wantType = currentType();
      var wantRarity = currentRarity();

      // Track rows first: the album pane's "does this release still have a
      // match" test reads the result rather than recomputing it.
      document.querySelectorAll(".wb-panel").forEach(function (panel) {
        var shown = 0;
        panel.querySelectorAll("tbody tr[data-track-rarity]").forEach(function (row) {
          var on = wantRarity === "all" || row.getAttribute("data-track-rarity") === wantRarity;
          row.hidden = !on;
          if (on) shown++;
        });
        var table = panel.querySelector(".table-tracks");
        var empty = panel.querySelector(".wb-no-tracks");
        if (table) table.hidden = shown === 0;
        if (empty) empty.hidden = shown !== 0;
      });

      var visible = [];
      tabs.forEach(function (tab) {
        var okType = wantType === "all" || tab.getAttribute("data-album-type") === wantType;
        var rarities = (tab.getAttribute("data-album-rarities") || "").split(/\s+/);
        var okRarity = wantRarity === "all" || rarities.indexOf(wantRarity) !== -1;
        var on = okType && okRarity;
        tab.hidden = !on;
        if (on) visible.push(tab);
      });
      if (note) note.hidden = visible.length > 0;

      // Never leave the track pane showing a release the list no longer offers.
      var selected = pane.querySelector('.wb-album[aria-selected="true"]');
      if (visible.length && (!selected || selected.hidden)) visible[0].click();
    }

    typeButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        typeButtons.forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        apply();
      });
    });
    if (raritySelect) raritySelect.addEventListener("change", apply);

    // Editing a rarity changes what the filter should match, so keep the tab's
    // rarity set and the row's marker in step with the saved value.
    document.addEventListener("gb:rarity-saved", function (e) {
      var row = e.detail.row;
      if (row) row.setAttribute("data-track-rarity", e.detail.rarity);
      var panel = row && row.closest(".wb-panel");
      var tab = panel && document.getElementById("tab-" + panel.id.slice("panel-".length));
      if (tab) {
        var set = [];
        panel.querySelectorAll("tbody tr[data-track-rarity]").forEach(function (r) {
          var v = r.getAttribute("data-track-rarity");
          if (set.indexOf(v) === -1) set.push(v);
        });
        tab.setAttribute("data-album-rarities", set.join(" "));
      }
      apply();
    });
  })();

  /* ---- Sortable tables --------------------------------------------------- */
  function cellValue(row, index, kind) {
    var cell = row.cells[index];
    if (!cell) return kind === "number" ? -Infinity : "";
    var raw = (cell.getAttribute("data-value") || cell.textContent || "").trim();
    if (kind === "number") {
      var n = parseFloat(raw.replace(/[^0-9.\-]/g, ""));
      return isNaN(n) ? -Infinity : n;
    }
    return raw.toLowerCase();
  }

  document.querySelectorAll("table[data-sortable]").forEach(function (table) {
    var body = table.tBodies[0];
    if (!body) return;

    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      var index = th.cellIndex;
      var kind = th.getAttribute("data-sort");
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");

      function sort() {
        // Placeholder rows (empty state) must never be reordered.
        var rows = Array.prototype.filter.call(body.rows, function (r) {
          return !r.querySelector(".empty-cell");
        });
        if (!rows.length) return;

        var asc = th.getAttribute("aria-sort") !== "ascending";
        table.querySelectorAll("th[data-sort]").forEach(function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");

        rows.sort(function (a, b) {
          var av = cellValue(a, index, kind);
          var bv = cellValue(b, index, kind);
          if (av < bv) return asc ? -1 : 1;
          if (av > bv) return asc ? 1 : -1;
          return 0;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      }

      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); }
      });
    });
  });

  /* ---- Search: live filter + clear button -------------------------------- */
  function applyFilter(input) {
    var sel = input.getAttribute("data-filter");
    if (!sel) return;
    var table = document.querySelector(sel);
    if (!table || !table.tBodies[0]) return;

    var counterSel = input.getAttribute("data-filter-count");
    var counter = counterSel ? document.querySelector(counterSel) : null;
    var needle = input.value.trim().toLowerCase();
    var shown = 0;

    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      if (row.querySelector(".empty-cell")) return;
      var match = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
      row.hidden = !match;
      if (match) shown++;
    });
    if (counter) counter.textContent = shown;
  }

  document.querySelectorAll("[data-search-clear]").forEach(function (input) {
    var wrap = input.closest(".search");
    var clearBtn = wrap && wrap.querySelector("[data-search-clear-btn]");

    function sync() { if (clearBtn) clearBtn.hidden = input.value === ""; }

    input.addEventListener("input", function () {
      sync();
      applyFilter(input);
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        input.value = "";
        sync();
        applyFilter(input);
        // Clearing is immediate and hands focus straight back to the field.
        input.focus();
        // A committed server-side query lives in the URL, so clearing it has to
        // go back to the unfiltered page rather than only emptying the box.
        var form = input.form;
        if (form && form.method.toLowerCase() === "get" &&
            new URLSearchParams(window.location.search).has(input.name)) {
          form.submit();
        }
      });
    }

    sync();
  });

  // Filter-only inputs that are not part of a clearable search wrapper.
  document.querySelectorAll("[data-filter]:not([data-search-clear])").forEach(function (input) {
    input.addEventListener("input", function () { applyFilter(input); });
  });

  /* ---- Busy submit buttons ----------------------------------------------- */
  document.querySelectorAll("form[data-busy-submit]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector('button[type="submit"], button:not([type])');
      if (btn) {
        btn.setAttribute("data-busy", "1");
        btn.disabled = true;
      }
      var hint = form.querySelector(".import-hint");
      var note = form.querySelector("[data-busy-note]");
      if (hint) hint.hidden = true;
      if (note) note.hidden = false;
    });
  });

  /* ---- Broken album art -------------------------------------------------- */
  // Spotify image URLs expire; swap a dead <img> for the shared fallback glyph
  // instead of showing a browser-default broken image.
  var FALLBACK_SVG =
    '<svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true"><line x1="2" x2="22" y1="2" y2="22"/>' +
    '<path d="M10.41 10.41a2 2 0 1 1-2.83-2.83"/><line x1="13.5" x2="6" y1="13.5" y2="21"/>' +
    '<line x1="18" x2="21" y1="12" y2="15"/>' +
    '<path d="M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59"/>' +
    '<path d="M21 15V5a2 2 0 0 0-2-2H9"/></svg>';

  document.querySelectorAll(".art img").forEach(function (img) {
    img.addEventListener("error", function () {
      var box = img.closest(".art");
      if (!box || box.querySelector(".art-fallback")) { img.remove(); return; }
      // A mosaic tile that dies just drops out; only an emptied box gets a glyph.
      img.remove();
      if (!box.querySelector("img")) {
        box.classList.remove("mosaic-1", "mosaic-2", "mosaic-3", "mosaic-4");
        var ph = document.createElement("span");
        ph.className = "art-fallback";
        ph.innerHTML = FALLBACK_SVG;
        box.appendChild(ph);
      }
    });
  });

  /* ---- Import progress --------------------------------------------------- */
  // The import page arrives fully rendered from the job as it stood; this keeps
  // it current by polling the job until it finishes.
  var importJob = document.querySelector("[data-import-url]");
  if (importJob) {
    var importUrl = importJob.getAttribute("data-import-url");
    var find = function (sel) { return document.querySelector(sel); };
    var setText = function (sel, text) { var el = find(sel); if (el) el.textContent = text; };

    var renderImport = function (job) {
      var state = job.active ? "active" : job.phase;
      importJob.setAttribute("data-state", state);
      importJob.querySelectorAll("[data-phase-icon]").forEach(function (el) {
        el.hidden = el.getAttribute("data-phase-icon") !== state;
      });
      var label = job.phase_label || "";
      setText("[data-import-phase]", label.charAt(0).toUpperCase() + label.slice(1));
      setText("[data-import-count]",
              job.phase === "tracks" && job.total ? job.done + " / " + job.total + " releases" : "");
      setText("[data-import-eta]", job.eta ? "About " + job.eta + "s left" : "");
      var bar = find("[data-import-bar]");
      if (bar) bar.style.width = job.percent + "%";
      var meter = importJob.querySelector("[role=progressbar]");
      if (meter) meter.setAttribute("aria-valuenow", job.percent);
      if (job.artist) setText("[data-import-title]", job.artist);
      setText('[data-stat="albums"]', job.releases.album);
      setText('[data-stat="singles"]', job.releases.single);
      setText('[data-stat="tracks"]', job.tracks.album + job.tracks.single);
      setText('[data-stat="new"]', job.result ? job.result.new_songs : "—");

      var problems = find("[data-import-problems]");
      var list = find("[data-import-errors]");
      if (problems && list) {
        problems.hidden = !job.errors.length;
        list.textContent = "";
        job.errors.forEach(function (text) {
          var li = document.createElement("li");
          li.textContent = text;
          list.appendChild(li);
        });
      }

      var actions = find("[data-import-actions]");
      var open = find("[data-import-open]");
      if (actions) actions.hidden = job.phase !== "done";
      if (open && job.artist_url) {
        open.href = job.artist_url;
        open.textContent = "Open " + job.artist;
      }
    };

    var pollImport = function () {
      fetch(importUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (res) {
          // A redirect means the session ended and we were sent to the login.
          if (res.redirected) throw new Error("login");
          if (res.status === 404) throw new Error("gone");
          if (!res.ok) throw new Error("retry");
          return res.json();
        })
        .then(function (job) {
          renderImport(job);
          if (job.active) setTimeout(pollImport, 1000);
        })
        .catch(function (err) {
          var why = err && err.message;
          if (why === "gone") {
            setText("[data-import-phase]", "This import is no longer available");
          } else if (why === "login") {
            setText("[data-import-phase]", "Signed out — refresh the page to log in again");
          } else {
            setTimeout(pollImport, 3000);   // a blip: try again a little later
          }
        });
    };
    if (importJob.getAttribute("data-state") === "active") setTimeout(pollImport, 1000);
  }

  /* ---- "/" focuses the page search --------------------------------------- */
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea" || e.target.isContentEditable) return;
    var box = document.querySelector("[data-search-focus]");
    if (box) { e.preventDefault(); box.focus(); box.select(); }
  });
})();
