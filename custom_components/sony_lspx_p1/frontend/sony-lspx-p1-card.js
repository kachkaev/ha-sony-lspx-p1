/**
 * Dashboard card for the Sony LSPX-P1 integration.
 *
 * Mirrors the control panel of Sony's smartphone app (Volume, Brightness + Auto,
 * Focus + Auto, Keystone + Reset, Rotate screen + Auto) using the integration's
 * entities and the dashboard theme's colours. Three layouts:
 *   compact  - one row: name, state, battery, volume, power
 *   standard - the app's control panel (default)
 *   full     - standard plus picture settings, xLOUD and charging
 *
 *   type: custom:sony-lspx-p1-card
 *   entity: media_player.lspx_p1_6320
 *   layout: standard
 *
 * Served by the integration; no build step and no dependencies.
 */

const CARD_TYPE = "sony-lspx-p1-card";
const DOMAIN = "sony_lspx_p1";

const WORDS = {
  en: { auto: "Auto", reset: "Reset", volume: "Volume", picture: "Picture", off: "Standby", unavailable: "Unavailable" },
  de: { auto: "Auto", reset: "Zurücksetzen", volume: "Lautstärke", picture: "Bild", off: "Standby", unavailable: "Nicht verfügbar" },
  fr: { auto: "Auto", reset: "Réinitialiser", volume: "Volume", picture: "Image", off: "Veille", unavailable: "Indisponible" },
  es: { auto: "Auto", reset: "Restablecer", volume: "Volumen", picture: "Imagen", off: "En espera", unavailable: "No disponible" },
  it: { auto: "Auto", reset: "Ripristina", volume: "Volume", picture: "Immagine", off: "Standby", unavailable: "Non disponibile" },
  nl: { auto: "Auto", reset: "Herstellen", volume: "Volume", picture: "Beeld", off: "Stand-by", unavailable: "Niet beschikbaar" },
  pl: { auto: "Auto", reset: "Resetuj", volume: "Głośność", picture: "Obraz", off: "Czuwanie", unavailable: "Niedostępny" },
  "pt-BR": { auto: "Auto", reset: "Redefinir", volume: "Volume", picture: "Imagem", off: "Em espera", unavailable: "Indisponível" },
  sv: { auto: "Auto", reset: "Återställ", volume: "Volym", picture: "Bild", off: "Viloläge", unavailable: "Inte tillgänglig" },
  da: { auto: "Auto", reset: "Nulstil", volume: "Lydstyrke", picture: "Billede", off: "Standby", unavailable: "Utilgængelig" },
  nb: { auto: "Auto", reset: "Tilbakestill", volume: "Volum", picture: "Bilde", off: "Hvilemodus", unavailable: "Utilgjengelig" },
  cs: { auto: "Auto", reset: "Resetovat", volume: "Hlasitost", picture: "Obraz", off: "Pohotovost", unavailable: "Nedostupné" },
  uk: { auto: "Авто", reset: "Скинути", volume: "Гучність", picture: "Зображення", off: "Очікування", unavailable: "Недоступно" },
  ru: { auto: "Авто", reset: "Сброс", volume: "Громкость", picture: "Изображение", off: "Ожидание", unavailable: "Недоступно" },
  "zh-Hans": { auto: "自动", reset: "重置", volume: "音量", picture: "画面", off: "待机", unavailable: "不可用" },
  ja: { auto: "自動", reset: "リセット", volume: "音量", picture: "画質", off: "スタンバイ", unavailable: "利用不可" },
};

// Entity keys (translation keys used by the integration) -> fallback entity_id suffixes.
const KEYS = {
  projector: [],
  brightness: ["_brightness"],
  focus: ["_focus"],
  keystone: ["_keystone"],
  auto_brightness: ["_auto_brightness"],
  auto_focus: ["_auto_focus"],
  auto_rotation: ["_auto_rotation"],
  xloud: ["_xloud"],
  rotation: ["_screen_rotation", "_rotation"],
  picture_mode: ["_picture_mode"],
  picture_saturation: ["_picture_saturation"],
  picture_brightness: ["_picture_brightness"],
  picture_contrast: ["_picture_contrast"],
  picture_sharpness: ["_picture_sharpness"],
  white_balance_red: ["_white_balance_red"],
  white_balance_green: ["_white_balance_green"],
  white_balance_blue: ["_white_balance_blue"],
  keystone_reset: ["_reset_keystone"],
  picture_reset: ["_reset_picture_settings"],
  charge_state: ["_charge_state"],
  battery: ["_battery"],
};

const PICTURE_SLIDERS = [
  ["picture_saturation", "mdi:palette"],
  ["picture_brightness", "mdi:brightness-5"],
  ["picture_contrast", "mdi:contrast-box"],
  ["picture_sharpness", "mdi:image-filter-hdr"],
  ["white_balance_red", "mdi:alpha-r-circle-outline"],
  ["white_balance_green", "mdi:alpha-g-circle-outline"],
  ["white_balance_blue", "mdi:alpha-b-circle-outline"],
];

const STYLE = `
  :host { display: block; }
  ha-card { padding: 4px 0 8px; overflow: hidden; }
  .header { display: flex; align-items: center; gap: 12px; padding: 12px 16px 4px; }
  .header .icon { color: var(--state-icon-color, var(--paper-item-icon-color)); }
  .header .icon.on { color: var(--primary-color); }
  .titles { flex: 1; min-width: 0; }
  .name { font-size: 16px; font-weight: 500; color: var(--primary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-size: 13px; color: var(--secondary-text-color); display: flex; gap: 8px; align-items: center; }
  .sub ha-icon { --mdc-icon-size: 16px; }
  .section { padding: 8px 16px; border-top: 1px solid var(--divider-color); }
  .section:first-of-type { border-top: none; }
  .section-head { display: flex; align-items: center; justify-content: space-between; min-height: 32px; }
  .title { color: var(--primary-color); font-weight: 500; font-size: 15px; }
  .row { display: flex; align-items: center; gap: 4px; min-height: 44px; }
  .row input[type=range] { flex: 1; min-width: 60px; }
  .value { min-width: 40px; text-align: right; font-variant-numeric: tabular-nums; color: var(--secondary-text-color); font-size: 13px; }
  input[type=range] { accent-color: var(--primary-color); height: 28px; cursor: pointer; background: transparent; }
  input[type=range]:disabled { cursor: default; opacity: 0.45; }
  button { font: inherit; color: var(--primary-text-color); background: none; border: none; cursor: pointer; border-radius: 50%; width: 40px; height: 40px; display: inline-flex; align-items: center; justify-content: center; flex: none; }
  button:hover:not(:disabled) { background: color-mix(in srgb, var(--primary-text-color) 8%, transparent); }
  button:disabled { opacity: 0.4; cursor: default; }
  button.power.on { color: var(--primary-color); }
  button.text { width: auto; border-radius: 8px; height: 32px; padding: 0 14px; border: 1px solid var(--divider-color); color: var(--secondary-text-color); font-size: 14px; }
  label.auto { display: inline-flex; align-items: center; gap: 6px; color: var(--secondary-text-color); font-size: 14px; cursor: pointer; }
  label.auto input { accent-color: var(--primary-color); width: 18px; height: 18px; margin: 0; cursor: pointer; }
  .segments { display: flex; border: 1px solid var(--divider-color); border-radius: 10px; overflow: hidden; margin: 6px 0 4px; }
  .segments button { flex: 1; border-radius: 0; height: 40px; width: auto; font-size: 18px; color: var(--primary-text-color); }
  .segments button + button { border-left: 1px solid var(--divider-color); }
  .segments button.selected { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .segments.small { flex-wrap: wrap; gap: 1px; background: var(--divider-color); }
  .segments.small button { flex: 1 1 auto; white-space: nowrap; padding: 0 6px; font-size: 12.5px; height: 34px;
    border-left: none; background: var(--ha-card-background, var(--card-background-color, #fff)); }
  .segments.small button.selected { background: var(--primary-color); }
  .flip { display: inline-block; transform: scaleX(-1) scaleY(-1); }
  .keystone-zero { position: relative; flex: 1; display: flex; }
  .keystone-zero::before { content: ""; position: absolute; left: 50%; top: -2px; width: 4px; height: 4px; margin-left: -2px; border-radius: 50%; background: var(--secondary-text-color); }
  .keystone-zero input { flex: 1; }
  .sliders .row { min-height: 40px; }
  .sliders ha-icon { color: var(--secondary-text-color); margin-right: 8px; }
  .sliders .label { width: 110px; flex: none; color: var(--primary-text-color); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .switch-row { display: flex; align-items: center; justify-content: space-between; min-height: 44px; }
  .dim { opacity: 0.55; }
  .compact { display: flex; align-items: center; gap: 8px; padding: 8px 8px 8px 16px; }
  .compact .titles { flex: 0 1 auto; max-width: 45%; }
  .compact input[type=range] { flex: 1; min-width: 60px; }
  .warning { padding: 16px; color: var(--error-color); }
`;

class SonyLspxP1Card extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { filter: { domain: "media_player", integration: DOMAIN } } } },
        {
          name: "layout",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "compact", label: "Compact" },
                { value: "standard", label: "Standard" },
                { value: "full", label: "Full" },
              ],
            },
          },
        },
      ],
    };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass.entities || {}).find(
      (e) => e.platform === DOMAIN && e.entity_id.startsWith("media_player."),
    );
    return { entity: entity ? entity.entity_id : "", layout: "standard" };
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choose the projector's media player entity");
    this._config = { layout: "standard", ...config };
    this._structureKey = null;
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return { compact: 1, standard: 7, full: 12 }[this._config?.layout] ?? 7;
  }

  getGridOptions() {
    const rows = { compact: 1, standard: 7, full: 12 }[this._config?.layout] ?? 7;
    return { columns: 12, min_columns: 6, rows, min_rows: rows };
  }

  // --- entity lookup -------------------------------------------------------

  _t(word) {
    const lang = this._hass?.locale?.language || this._hass?.language || "en";
    const table = WORDS[lang] || WORDS[lang.split("-")[0]] || WORDS.en;
    return table[word] || WORDS.en[word];
  }

  _findEntities() {
    const hass = this._hass;
    const main = hass.entities?.[this._config.entity];
    const found = { projector: this._config.entity };
    if (!main) return found;
    const siblings = Object.values(hass.entities).filter((e) => e.device_id === main.device_id && e.platform === DOMAIN);
    for (const [key, suffixes] of Object.entries(KEYS)) {
      if (key === "projector") continue;
      let match = siblings.find((e) => e.translation_key === key);
      if (!match && key === "battery") {
        match = siblings.find((e) => hass.states[e.entity_id]?.attributes.device_class === "battery");
      }
      if (!match) match = siblings.find((e) => suffixes.some((s) => e.entity_id.endsWith(s)));
      if (match) found[key] = match.entity_id;
    }
    return found;
  }

  _state(key) {
    const id = this._ids?.[key];
    return id ? this._hass.states[id] : undefined;
  }

  _available(key) {
    const s = this._state(key);
    return !!s && s.state !== "unavailable" && s.state !== "unknown";
  }

  _label(key) {
    const s = this._state(key);
    if (!s) return "";
    const full = s.attributes.friendly_name || "";
    const device = this._deviceName();
    return device && full.startsWith(device) ? full.slice(device.length).trim() : full;
  }

  _deviceName() {
    const main = this._hass.entities?.[this._config.entity];
    const device = main && this._hass.devices?.[main.device_id];
    return device ? device.name_by_user || device.name : "";
  }

  _call(domain, service, key, data = {}) {
    const entity_id = this._ids[key];
    if (!entity_id) return;
    this._hass.callService(domain, service, { entity_id, ...data });
  }

  // --- rendering -----------------------------------------------------------

  _update() {
    if (!this._config || !this._hass) return;
    this._ids = this._findEntities();
    const key = `${this._config.layout}|${Object.values(this._ids).join(",")}`;
    if (key !== this._structureKey) {
      this._structureKey = key;
      this._build();
    }
    this._refresh();
  }

  _build() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const layout = this._config.layout;
    const parts = [];
    if (!this._hass.states[this._config.entity]) {
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card><div class="warning">Entity not found: ${this._config.entity}</div></ha-card>`;
      return;
    }
    if (layout === "compact") {
      parts.push(`
        <div class="compact">
          <ha-icon class="icon" data-ref="icon" icon="mdi:projector"></ha-icon>
          <div class="titles"><div class="name" data-ref="name"></div><div class="sub" data-ref="sub"></div></div>
          <input type="range" data-slider="volume" min="0" max="100" step="1" aria-label="${this._t("volume")}">
          <button class="power" data-action="power"><ha-icon icon="mdi:power"></ha-icon></button>
        </div>`);
    } else {
      parts.push(`
        <div class="header">
          <ha-icon class="icon" data-ref="icon" icon="mdi:projector"></ha-icon>
          <div class="titles"><div class="name" data-ref="name"></div><div class="sub" data-ref="sub"></div></div>
          <button class="power" data-action="power"><ha-icon icon="mdi:power"></ha-icon></button>
        </div>`);
      parts.push(this._sliderSection("volume", this._t("volume"), "mdi:minus-circle-outline", "mdi:plus-circle-outline"));
      parts.push(this._sliderSection("brightness", null, "mdi:minus-circle-outline", "mdi:plus-circle-outline", "auto_brightness"));
      parts.push(this._sliderSection("focus", null, "mdi:chevron-left", "mdi:chevron-right", "auto_focus"));
      parts.push(this._sliderSection("keystone", null, "mdi:chevron-left", "mdi:chevron-right", null, "keystone_reset"));
      if (this._ids.rotation) {
        parts.push(`
          <div class="section">
            <div class="section-head"><span class="title" data-label="rotation"></span>${this._autoBox("auto_rotation")}</div>
            <div class="segments" data-segments="rotation">
              <button data-option="normal">R</button>
              <button data-option="rotate_180"><span class="flip">R</span></button>
            </div>
          </div>`);
      }
      if (layout === "full") {
        if (this._ids.picture_mode) {
          parts.push(`
            <div class="section">
              <div class="section-head"><span class="title" data-label="picture_mode"></span></div>
              <div class="segments small" data-segments="picture_mode"></div>
              <div class="sliders" data-ref="picture-sliders">
                ${PICTURE_SLIDERS.filter(([k]) => this._ids[k])
                  .map(
                    ([k, icon]) => `
                  <div class="row"><ha-icon icon="${icon}"></ha-icon><span class="label" data-label="${k}"></span>
                    <input type="range" data-slider="${k}"><span class="value" data-value="${k}"></span></div>`,
                  )
                  .join("")}
              </div>
            </div>`);
        }
        if (this._ids.xloud) {
          parts.push(`
            <div class="section"><div class="switch-row"><span class="title" data-label="xloud"></span>
              <label class="auto"><input type="checkbox" data-toggle="xloud"></label></div></div>`);
        }
        // Like the app: resetting the custom picture settings comes last.
        if (this._ids.picture_reset) {
          parts.push(`
            <div class="section" data-ref="picture-reset-section"><div class="switch-row">
              <span class="title" data-label="picture_reset"></span>
              <button class="text" data-action="picture_reset">${this._t("reset")}</button></div></div>`);
        }
      }
    }
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>${parts.join("")}</ha-card>`;
    this._bind();
  }

  _autoBox(key) {
    if (!this._ids[key]) return "";
    return `<label class="auto">${this._t("auto")}<input type="checkbox" data-toggle="${key}"></label>`;
  }

  _sliderSection(key, title, minusIcon, plusIcon, autoKey = null, resetKey = null) {
    if (key !== "volume" && !this._ids[key]) return "";
    const right = autoKey
      ? this._autoBox(autoKey)
      : resetKey && this._ids[resetKey]
        ? `<button class="text" data-action="${resetKey}">${this._t("reset")}</button>`
        : "";
    const slider = `<input type="range" data-slider="${key}">`;
    return `
      <div class="section">
        <div class="section-head"><span class="title" ${title ? "" : `data-label="${key}"`}>${title || ""}</span>${right}</div>
        <div class="row">
          <button data-step="${key}" data-dir="-1"><ha-icon icon="${minusIcon}"></ha-icon></button>
          ${key === "keystone" ? `<div class="keystone-zero">${slider}</div>` : slider}
          <button data-step="${key}" data-dir="1"><ha-icon icon="${plusIcon}"></ha-icon></button>
          <span class="value" data-value="${key}"></span>
        </div>
      </div>`;
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-action=power]").forEach((b) =>
      b.addEventListener("click", () => {
        const on = this._state("projector")?.state === "on";
        this._call("media_player", on ? "turn_off" : "turn_on", "projector");
      }),
    );
    root.querySelectorAll("[data-action=keystone_reset], [data-action=picture_reset]").forEach((b) =>
      b.addEventListener("click", () => this._call("button", "press", b.dataset.action)),
    );
    root.querySelectorAll("input[data-slider]").forEach((input) => {
      input.addEventListener("input", () => {
        const v = root.querySelector(`[data-value="${input.dataset.slider}"]`);
        if (v) v.textContent = input.value;
      });
      input.addEventListener("change", () => this._setValue(input.dataset.slider, Number(input.value)));
    });
    root.querySelectorAll("button[data-step]").forEach((b) =>
      b.addEventListener("click", () => {
        const key = b.dataset.step;
        const input = root.querySelector(`input[data-slider="${key}"]`);
        const step = { volume: 5, brightness: 5, focus: 5, keystone: 1 }[key] || 1;
        const next = Math.min(Number(input.max), Math.max(Number(input.min), Number(input.value) + step * Number(b.dataset.dir)));
        input.value = next;
        this._setValue(key, next);
      }),
    );
    root.querySelectorAll("input[data-toggle]").forEach((box) =>
      box.addEventListener("change", () => this._call("switch", box.checked ? "turn_on" : "turn_off", box.dataset.toggle)),
    );
    root.querySelectorAll("[data-segments=rotation] button").forEach((b) =>
      b.addEventListener("click", () => this._call("select", "select_option", "rotation", { option: b.dataset.option })),
    );
  }

  _setValue(key, value) {
    if (key === "volume") this._call("media_player", "volume_set", "projector", { volume_level: value / 100 });
    else this._call("number", "set_value", key, { value });
  }

  _refresh() {
    const root = this.shadowRoot;
    if (!root || !root.querySelector("ha-card")) return;
    const projector = this._state("projector");
    if (!projector) return;
    const reachable = projector.state !== "unavailable";
    const on = projector.state === "on";

    const nameEl = root.querySelector("[data-ref=name]");
    if (nameEl) nameEl.textContent = this._config.name || this._deviceName() || projector.attributes.friendly_name;
    const sub = root.querySelector("[data-ref=sub]");
    if (sub) {
      const battery = this._state("battery");
      const charge = this._state("charge_state");
      const status = !reachable ? this._t("unavailable") : on ? projector.attributes.source || this._hass.formatEntityState?.(projector) || "On" : this._t("off");
      const batteryIcon = charge?.state === "charging" ? "mdi:battery-charging" : charge?.state === "full" ? "mdi:battery-check" : "mdi:battery";
      sub.innerHTML = `<span>${status}</span>${
        battery && battery.state !== "unavailable" ? `<span><ha-icon icon="${batteryIcon}"></ha-icon> ${battery.state}%</span>` : ""
      }`;
    }
    root.querySelector("[data-ref=icon]")?.classList.toggle("on", on);
    root.querySelectorAll("[data-action=power]").forEach((b) => {
      b.classList.toggle("on", on);
      b.disabled = !reachable;
    });

    root.querySelectorAll("[data-label]").forEach((el) => (el.textContent = this._label(el.dataset.label)));

    // Sliders
    root.querySelectorAll("input[data-slider]").forEach((input) => {
      const key = input.dataset.slider;
      let value, min, max, enabled;
      if (key === "volume") {
        min = 0;
        max = 100;
        const level = projector.attributes.volume_level;
        value = level == null ? null : Math.round(level * 100);
        enabled = on;
      } else {
        const s = this._state(key);
        if (!s) return;
        min = s.attributes.min;
        max = s.attributes.max;
        value = Number(s.state);
        enabled = on && this._available(key);
      }
      input.min = min;
      input.max = max;
      input.step = 1;
      if (this.shadowRoot.activeElement !== input && value != null && !Number.isNaN(value)) input.value = value;
      input.disabled = !enabled;
      const v = root.querySelector(`[data-value="${key}"]`);
      if (v) v.textContent = enabled && value != null && !Number.isNaN(value) ? value : "–";
      root.querySelectorAll(`button[data-step="${key}"]`).forEach((b) => (b.disabled = !enabled));
    });

    // Auto checkboxes and toggles; the manual slider is dimmed while Auto is on
    root.querySelectorAll("input[data-toggle]").forEach((box) => {
      const s = this._state(box.dataset.toggle);
      box.checked = s?.state === "on";
      box.disabled = !on || !this._available(box.dataset.toggle);
    });
    for (const [slider, auto] of [["brightness", "auto_brightness"], ["focus", "auto_focus"]]) {
      const input = root.querySelector(`input[data-slider="${slider}"]`);
      input?.classList.toggle("dim", this._state(auto)?.state === "on");
    }

    // Rotation
    const rotation = this._state("rotation");
    root.querySelectorAll("[data-segments=rotation] button").forEach((b) => {
      b.classList.toggle("selected", rotation?.state === b.dataset.option);
      b.disabled = !on || !this._available("rotation");
    });

    // Picture mode chips (built from the select's options, labelled by the entity's translations)
    const modeBox = root.querySelector("[data-segments=picture_mode]");
    const mode = this._state("picture_mode");
    if (modeBox && mode) {
      const options = mode.attributes.options || [];
      if (modeBox.dataset.options !== options.join(",")) {
        modeBox.dataset.options = options.join(",");
        modeBox.innerHTML = options
          .map((o) => `<button data-option="${o}">${this._optionLabel(mode, o)}</button>`)
          .join("");
        modeBox.querySelectorAll("button").forEach((b) =>
          b.addEventListener("click", () => this._call("select", "select_option", "picture_mode", { option: b.dataset.option })),
        );
      }
      modeBox.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("selected", mode.state === b.dataset.option);
        b.disabled = !on || !this._available("picture_mode");
      });
      const custom = String(mode.state).startsWith("custom");
      const sliders = root.querySelector("[data-ref=picture-sliders]");
      if (sliders) sliders.style.display = custom ? "" : "none";
      const resetSection = root.querySelector("[data-ref=picture-reset-section]");
      if (resetSection) resetSection.style.display = custom ? "" : "none";
      const reset = root.querySelector("[data-action=picture_reset]");
      if (reset) reset.disabled = !on || !this._available("picture_reset");
    }
    const keystoneReset = root.querySelector("[data-action=keystone_reset]");
    if (keystoneReset) keystoneReset.disabled = !on || !this._available("keystone_reset");
  }

  _optionLabel(stateObj, option) {
    // Translated state names come from the integration's translation files.
    const translated = this._hass.formatEntityState?.(stateObj, option);
    if (translated && translated !== option) return translated;
    return option.replace(/_(\d)$/, " $1").replace(/^./, (c) => c.toUpperCase()).replace("_", " ");
  }
}

// The integration loads this file very early, before Home Assistant's frontend replaces
// window.customElements with its scoped registry. Defining the card before that would put
// it where dashboards cannot see it, so wait until the frontend's own app element is
// visible through the current registry (or give up waiting after a while).
function registerCard(attempt = 0) {
  if (!customElements.get("home-assistant") && attempt < 200) {
    setTimeout(() => registerCard(attempt + 1), 50);
    return;
  }
  if (customElements.get(CARD_TYPE)) return;
  customElements.define(CARD_TYPE, SonyLspxP1Card);
  window.customCards = window.customCards || [];
  if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
    window.customCards.push({
      type: CARD_TYPE,
      name: "Sony LSPX-P1 Projector",
      description: "Projector controls laid out like Sony's app: volume, brightness, focus, keystone and screen rotation.",
      preview: true,
      documentationURL: "https://github.com/kachkaev/ha-sony-lspx-p1",
    });
  }
}

registerCard();
