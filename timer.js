"use strict";

/** 時刻文字列 (MM:SS / HH:MM:SS) を秒に変換。不正なら null */
function parseTime(text) {
  const m = /^(?:(\d+):)?(\d{1,2}):(\d{1,2})$/.exec((text || "").trim());
  if (!m) return null;
  const h = m[1] === undefined ? 0 : Number(m[1]);
  const min = Number(m[2]);
  const sec = Number(m[3]);
  if (min > 59 || sec > 59) return null;
  return h * 3600 + min * 60 + sec;
}

function formatTime(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const p = (n) => String(n).padStart(2, "0");
  return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
}

/** CSV の解析と検証。列は見出し行で名前解決するため列追加に強い (例: candidate) */
class CsvLoader {
  static REQUIRED = ["time", "message"];

  static parseRows(text) {
    const rows = [];
    let row = [], field = "", quoted = false;
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; } else quoted = false;
        } else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") { row.push(field); field = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(field); field = "";
        rows.push(row); row = [];
      } else field += c;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    return rows.filter((r) => r.some((f) => f.trim() !== ""));
  }

  /** @returns {{events: object[], errors: string[]}} */
  static parse(text) {
    const errors = [];
    const rows = CsvLoader.parseRows(text);
    if (rows.length === 0) return { events: [], errors: ["CSVが空です"] };
    const header = rows[0].map((h) => h.trim().toLowerCase());
    const missing = CsvLoader.REQUIRED.filter((c) => !header.includes(c));
    if (missing.length) {
      return { events: [], errors: [`CSVの見出し行が不正です。不足している列: ${missing.join(", ")}`] };
    }
    const events = [];
    const seen = new Map();
    for (let i = 1; i < rows.length; i++) {
      const lineNo = i + 1;
      const r = rows[i];
      if (r.length > header.length) {
        errors.push(`${lineNo}行目: 列数が見出しより多くなっています`);
        continue;
      }
      const rec = {};
      header.forEach((h, k) => { rec[h] = (r[k] || "").trim(); });
      const seconds = parseTime(rec.time);
      if (seconds === null) {
        errors.push(`${lineNo}行目: time列が不正です ("${rec.time}")。MM:SS または HH:MM:SS で指定してください`);
        continue;
      }
      if (seen.has(seconds)) {
        errors.push(`${lineNo}行目: 同一時刻 (${formatTime(seconds)}) のイベントが重複しています (${seen.get(seconds)}行目)`);
        continue;
      }
      seen.set(seconds, lineNo);
      let duration = null;
      if (rec.duration) {
        duration = /^\d+(?:\.\d+)?$/.test(rec.duration) ? Number(rec.duration) : NaN;
        if (!(duration > 0)) {
          errors.push(`${lineNo}行目: duration列が不正です ("${rec.duration}")。0より大きい秒数で指定してください`);
          continue;
        }
      }
      events.push({
        time: seconds, message: rec.message, audio: rec.audio || "",
        candidate: rec.candidate || "", duration, line: lineNo,
      });
    }
    events.sort((a, b) => a.time - b.time);
    return { events, errors };
  }
}

/** 音声のプリロード・再生許可の取得・再生 */
class AudioManager {
  constructor(dir = "audio/") {
    this.dir = dir;
    this.clips = new Map();
  }

  /** 全音声をプリロード。読込失敗したファイル名一覧を返す */
  preload(fileNames) {
    this.clips.clear();
    const unique = [...new Set(fileNames.filter(Boolean))];
    return Promise.all(unique.map((name) => new Promise((resolve) => {
      const a = new Audio();
      a.preload = "auto";
      const done = (ok) => { a.onerror = a.oncanplaythrough = null; resolve(ok ? null : name); };
      a.oncanplaythrough = () => done(true);
      a.onerror = () => done(false);
      a.src = this.dir + encodeURIComponent(name);
      a.load();
      this.clips.set(name, a);
      setTimeout(() => done(a.readyState >= 3), 10000);
    })));
  }

  /** ユーザー操作内で呼び出し、各音声の再生許可を取得する */
  unlock() {
    for (const a of this.clips.values()) {
      a.muted = true;
      const p = a.play();
      const reset = () => { a.pause(); a.currentTime = 0; a.muted = false; };
      if (p && p.then) p.then(reset, () => { a.muted = false; }); else reset();
    }
  }

  play(name) {
    const a = this.clips.get(name);
    if (!a) return;
    a.muted = false;
    a.currentTime = 0;
    const p = a.play();
    if (p && p.catch) p.catch((e) => console.warn("audio play failed", name, e));
  }

  stopAll() {
    for (const a of this.clips.values()) { a.pause(); a.currentTime = 0; }
  }
}

/** 経過時間に応じてイベントを一度だけ発火する (ポインタ方式: 件数が多くても O(1)/tick) */
class EventScheduler {
  constructor(events = []) { this.setEvents(events); }
  setEvents(events) {
    this.events = [...events].sort((a, b) => a.time - b.time);
    this.reset();
  }
  reset() { this.next = 0; }
  /** elapsed 秒までに到達した未実行イベントを返す */
  due(elapsed) {
    const out = [];
    while (this.next < this.events.length && this.events[this.next].time <= elapsed) {
      out.push(this.events[this.next++]);
    }
    return out;
  }
  get finished() { return this.next >= this.events.length; }
}

/** 経過時間管理 (performance.now ベース) と画面更新 */
class TimerController {
  static TICK_MS = 50;

  constructor(scheduler, audio, ui) {
    this.scheduler = scheduler;
    this.audio = audio;
    this.ui = ui;
    this.state = "idle"; // idle | running | paused
    this.base = 0;       // 一時停止までの累積ms
    this.startedAt = 0;
    this.handle = null;
    this.clearAt = null; // メッセージを消す経過秒 (null = 消さない)
  }

  elapsedSec() {
    const ms = this.state === "running" ? this.base + performance.now() - this.startedAt : this.base;
    return ms / 1000;
  }

  start() {
    this.scheduler.reset();
    this.base = 0;
    this.clearAt = null;
    this.ui.setEvent(null);
    this.resume();
  }

  resume() {
    if (this.state === "running") return;
    this.state = "running";
    this.startedAt = performance.now();
    this.handle = setInterval(() => this.tick(), TimerController.TICK_MS);
    this.tick();
    this.ui.onState(this.state);
  }

  pause() {
    if (this.state !== "running") return;
    this.base += performance.now() - this.startedAt;
    this.state = "paused";
    clearInterval(this.handle);
    this.audio.stopAll();
    this.ui.onState(this.state);
  }

  stop() {
    clearInterval(this.handle);
    this.state = "idle";
    this.base = 0;
    this.audio.stopAll();
    this.scheduler.reset();
    this.clearAt = null;
    this.ui.setTime(0);
    this.ui.setEvent(null);
    this.ui.onState(this.state);
  }

  tick() {
    const t = this.elapsedSec();
    this.ui.setTime(t);
    const due = this.scheduler.due(t);
    if (due.length) {
      // 複数同時到達時(タブ遅延など)は最新のイベントのみ表示・再生
      const ev = due[due.length - 1];
      this.ui.setEvent(ev);
      this.clearAt = ev.duration ? ev.time + ev.duration : null;
      if (ev.audio) this.audio.play(ev.audio);
    }
    if (this.clearAt !== null && t >= this.clearAt) {
      this.clearAt = null;
      this.ui.clearMessage();
    }
  }
}

/** DOM 操作と全体の組み立て */
class App {
  constructor() {
    const $ = (id) => document.getElementById(id);
    this.el = {
      timer: $("timer"), message: $("message"), candidate: $("candidate"),
      errors: $("errors"), status: $("status"), file: $("csvFile"),
      load: $("btnLoad"), start: $("btnStart"), pause: $("btnPause"),
      resume: $("btnResume"), stop: $("btnStop"), full: $("btnFullscreen"),
    };
    this.audio = new AudioManager("audio/");
    this.scheduler = new EventScheduler();
    this.timer = new TimerController(this.scheduler, this.audio, {
      setTime: (t) => { const s = formatTime(t); if (this.el.timer.textContent !== s) this.el.timer.textContent = s; },
      setEvent: (ev) => this.showEvent(ev),
      clearMessage: () => { this.el.message.textContent = ""; }, // candidateは残す
      onState: (s) => this.updateButtons(s),
    });
    this.loaded = false;
    this.bind();
    this.updateButtons("idle");
  }

  bind() {
    const e = this.el;
    e.load.onclick = () => e.file.click();
    e.file.onchange = () => this.loadFile(e.file.files[0]);
    e.start.onclick = () => this.start();
    e.pause.onclick = () => this.timer.pause();
    e.resume.onclick = () => { this.audio.unlock(); this.timer.resume(); };
    e.stop.onclick = () => this.timer.stop();
    e.full.onclick = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen().catch((err) => this.showErrors([`全画面表示に失敗: ${err.message}`]));
    };
  }

  showEvent(ev) {
    this.el.message.textContent = ev ? ev.message : (this.loaded ? "待機中" : "CSVを読み込んでください");
    const cand = ev ? ev.candidate : "";
    this.el.candidate.hidden = !cand;
    this.el.candidate.textContent = cand;
  }

  showErrors(list, warn = false) {
    this.el.errors.hidden = list.length === 0;
    this.el.errors.classList.toggle("warn", warn);
    this.el.errors.textContent = list.join("\n");
  }

  updateButtons(state) {
    const e = this.el;
    e.load.disabled = state !== "idle";
    e.start.disabled = !(state === "idle" && this.loaded);
    e.pause.disabled = state !== "running";
    e.resume.disabled = state !== "paused";
    e.stop.disabled = state === "idle";
  }

  async loadFile(file) {
    if (!file) return;
    this.loaded = false;
    this.updateButtons("idle");
    this.el.status.textContent = "読込中...";
    let text;
    try {
      text = new TextDecoder("utf-8").decode(await file.arrayBuffer());
    } catch (err) {
      this.showErrors([`CSVの読込に失敗しました: ${err.message}`]);
      this.el.status.textContent = "";
      return;
    }
    const { events, errors } = CsvLoader.parse(text);
    this.el.file.value = "";
    if (errors.length || events.length === 0) {
      if (!events.length && !errors.length) errors.push("有効なイベントがありません");
      this.showErrors(errors);
      this.el.status.textContent = "CSVエラー";
      this.timer.ui.setEvent(null);
      return;
    }
    this.scheduler.setEvents(events);
    this.events = events;
    this.showErrors([]);
    const failed = await this.audio.preload(events.map((ev) => ev.audio));
    const warns = failed.map((n) => `音声ファイルが見つかりません: audio/${n}`);
    if (warns.length) this.showErrors(warns, true);
    this.loaded = true;
    this.el.status.textContent = `${file.name}: ${events.length}件`;
    this.showEvent(null);
    this.updateButtons("idle");
  }

  start() {
    this.audio.unlock(); // ユーザー操作内で再生許可を取得
    this.timer.start();
  }
}

window.addEventListener("DOMContentLoaded", () => { window.app = new App(); });
