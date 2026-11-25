/**
 * Dashboard card for the Sony LSPX-P1 integration.
 *
 * Mirrors the control panel of Sony's smartphone app (Volume, Brightness + Auto,
 * Focus + Auto, Keystone + Reset, Rotate screen + Auto) using the integration's
 * entities and the dashboard theme's colours. Every layout starts with one line (icon,
 * name, battery, power) and a row of source chips (HDMI and the posters) that scrolls
 * sideways. Three layouts:
 *   compact  - header, sources and a volume slider
 *   standard - the app's control panel (default)
 *   full     - standard plus picture settings
 *
 * A short muted word under the name always tells the state (On, Starting up, Standby, Unavailable). The header's
 * layout depends only on the card's width, never on the state: on a narrow card (half width in a sections view)
 * the battery percentage and then the icon give way, so the name keeps some room.
 *
 * Tapping the icon and name opens the full controls in a dialog (from the compact and
 * standard layouts) or Home Assistant's own entity dialog (from the full layout). In the
 * dialog, the card's first line becomes the dialog's bar: close, name, battery, power and
 * buttons for the entity's history and settings. Tapping the battery opens the battery
 * entity's dialog.
 *
 *   type: custom:sony-lspx-p1-card
 *   entity: media_player.lspx_p1_6320
 *   layout: standard
 *
 * Served by the integration; no build step and no dependencies.
 */

const CARD_TYPE = "sony-lspx-p1-card";
const DOMAIN = "sony_lspx_p1";
// Sent by the card inside the details dialog when its close button is pressed.
const CLOSE_EVENT = `${CARD_TYPE}-close`;

const WORDS = {
  en: { auto: "Auto", reset: "Reset", volume: "Volume", on: "On", off: "Standby", starting: "Starting up", unavailable: "Unavailable", source: "Source", quieter: "Quieter", louder: "Louder" },
  de: { auto: "Auto", reset: "Zurücksetzen", volume: "Lautstärke", on: "Ein", off: "Standby", starting: "Startet", unavailable: "Nicht verfügbar", source: "Quelle", quieter: "Leiser", louder: "Lauter",
    posters: { Weather: "Wetter", clock: "Uhr {n}", "A day": "Ein Tag", Journey: "Reise", "Night dreams": "Träume", Window: "Fenster" } },
  fr: { auto: "Auto", reset: "Réinitialiser", volume: "Volume", on: "Allumé", off: "Veille", starting: "Démarrage", unavailable: "Indisponible", source: "Source", quieter: "Moins fort", louder: "Plus fort",
    posters: { Weather: "Météo", clock: "Horloge {n}", "A day": "Journée", Journey: "Voyage", "Night dreams": "Rêves nocturnes", Window: "Fenêtre" } },
  es: { auto: "Auto", reset: "Restablecer", volume: "Volumen", on: "Encendido", off: "En espera", starting: "Iniciando", unavailable: "No disponible", source: "Fuente", quieter: "Bajar volumen", louder: "Subir volumen",
    posters: { Weather: "Tiempo", clock: "Reloj {n}", "A day": "Un día", Journey: "Viaje", "Night dreams": "Sueños", Window: "Ventana" } },
  it: { auto: "Auto", reset: "Ripristina", volume: "Volume", on: "Acceso", off: "Standby", starting: "Avvio", unavailable: "Non disponibile", source: "Sorgente", quieter: "Abbassa volume", louder: "Alza volume",
    posters: { Weather: "Meteo", clock: "Orologio {n}", "A day": "Un giorno", Journey: "Viaggio", "Night dreams": "Sogni notturni", Window: "Finestra" } },
  nl: { auto: "Auto", reset: "Herstellen", volume: "Volume", on: "Aan", off: "Stand-by", starting: "Opstarten", unavailable: "Niet beschikbaar", source: "Bron", quieter: "Zachter", louder: "Harder",
    posters: { Weather: "Weer", clock: "Klok {n}", "A day": "Een dag", Journey: "Reis", "Night dreams": "Nachtdromen", Window: "Raam" } },
  pl: { auto: "Auto", reset: "Resetuj", volume: "Głośność", on: "Włączony", off: "Czuwanie", starting: "Uruchamianie", unavailable: "Niedostępny", source: "Źródło", quieter: "Ciszej", louder: "Głośniej",
    posters: { Weather: "Pogoda", clock: "Zegar {n}", "A day": "Jeden dzień", Journey: "Podróż", "Night dreams": "Nocne marzenia", Window: "Okno" } },
  "pt-BR": { auto: "Auto", reset: "Redefinir", volume: "Volume", on: "Ligado", off: "Em espera", starting: "Iniciando", unavailable: "Indisponível", source: "Fonte", quieter: "Diminuir volume", louder: "Aumentar volume",
    posters: { Weather: "Clima", clock: "Relógio {n}", "A day": "Um dia", Journey: "Jornada", "Night dreams": "Sonhos noturnos", Window: "Janela" } },
  sv: { auto: "Auto", reset: "Återställ", volume: "Volym", on: "På", off: "Viloläge", starting: "Startar", unavailable: "Inte tillgänglig", source: "Källa", quieter: "Sänk volymen", louder: "Höj volymen",
    posters: { Weather: "Väder", clock: "Klocka {n}", "A day": "En dag", Journey: "Resa", "Night dreams": "Nattdrömmar", Window: "Fönster" } },
  da: { auto: "Auto", reset: "Nulstil", volume: "Lydstyrke", on: "Til", off: "Standby", starting: "Starter", unavailable: "Utilgængelig", source: "Kilde", quieter: "Skru ned", louder: "Skru op",
    posters: { Weather: "Vejr", clock: "Ur {n}", "A day": "En dag", Journey: "Rejse", "Night dreams": "Natdrømme", Window: "Vindue" } },
  nb: { auto: "Auto", reset: "Tilbakestill", volume: "Volum", on: "På", off: "Hvilemodus", starting: "Starter", unavailable: "Utilgjengelig", source: "Kilde", quieter: "Skru ned", louder: "Skru opp",
    posters: { Weather: "Vær", clock: "Klokke {n}", "A day": "En dag", Journey: "Reise", "Night dreams": "Nattdrømmer", Window: "Vindu" } },
  cs: { auto: "Auto", reset: "Resetovat", volume: "Hlasitost", on: "Zapnuto", off: "Pohotovost", starting: "Spouštění", unavailable: "Nedostupné", source: "Zdroj", quieter: "Tišeji", louder: "Hlasitěji",
    posters: { Weather: "Počasí", clock: "Hodiny {n}", "A day": "Jeden den", Journey: "Cesta", "Night dreams": "Noční sny", Window: "Okno" } },
  uk: { auto: "Авто", reset: "Скинути", volume: "Гучність", on: "Увімкнено", off: "Очікування", starting: "Запуск", unavailable: "Недоступно", source: "Джерело", quieter: "Тихіше", louder: "Гучніше",
    posters: { Weather: "Погода", clock: "Годинник {n}", "A day": "Один день", Journey: "Подорож", "Night dreams": "Нічні мрії", Window: "Вікно" } },
  ru: { auto: "Авто", reset: "Сброс", volume: "Громкость", on: "Включено", off: "Ожидание", starting: "Запуск", unavailable: "Недоступно", source: "Источник", quieter: "Тише", louder: "Громче",
    posters: { Weather: "Погода", clock: "Часы {n}", "A day": "Один день", Journey: "Путешествие", "Night dreams": "Ночные грёзы", Window: "Окно" } },
  "zh-Hans": { auto: "自动", reset: "重置", volume: "音量", on: "开启", off: "待机", starting: "正在启动", unavailable: "不可用", source: "信号源", quieter: "降低音量", louder: "提高音量",
    posters: { Weather: "天气", clock: "时钟{n}", "A day": "一天", Journey: "旅程", "Night dreams": "夜之梦", Window: "窗" } },
  ja: { auto: "自動", reset: "リセット", volume: "音量", on: "オン", off: "スタンバイ", starting: "起動中", unavailable: "利用不可", source: "入力", quieter: "音量を下げる", louder: "音量を上げる",
    posters: { Weather: "天気", clock: "時計{n}", "A day": "A day", Journey: "Journey", "Night dreams": "Night dreams", Window: "Window" } },
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

// Screen rotation options (the select's, in its order) and their icons; MDI has none for a half turn, so
// "rotate_180" shows the picture turned. The entity's translated state name is the button's tooltip and
// accessible name.
const ROTATIONS = [
  ["normal", "mdi:image-outline"],
  ["rotate_180", "mdi:image-outline", "turned"],
  ["mirror_horizontal", "mdi:flip-horizontal"],
  ["mirror_vertical", "mdi:flip-vertical"],
];

const PICTURE_SLIDERS = [
  ["picture_saturation", "mdi:palette"],
  ["picture_brightness", "mdi:brightness-5"],
  ["picture_contrast", "mdi:contrast-box"],
  ["picture_sharpness", "mdi:image-filter-hdr"],
  ["white_balance_red", "mdi:alpha-r-circle-outline"],
  ["white_balance_green", "mdi:alpha-g-circle-outline"],
  ["white_balance_blue", "mdi:alpha-b-circle-outline"],
];

// Optimistic updates, as in Home Assistant's own controls: a control shows the chosen value straight
// away and keeps it while the projector catches up. It goes back to the projector's value if the
// call fails, or if the call has finished and no matching value arrives within HOLD_MS (Home
// Assistant's entity toggle waits 2 s). A source can take a few polls to show.
const HOLD_MS = 2000;
const SOURCE_HOLD_MS = 15000;
// A control still waiting after this long pulses, so it is clear the choice was registered.
const PULSE_AFTER_MS = 1000;
// Settings that affect each other share a lane: a lane sends one change at a time, in the order they were
// made, so for example "Auto off" lands before the brightness dragged right after it. Settings in
// different lanes (volume and brightness, say) are sent independently. Power waits for calls already in
// flight and every other lane waits for power; switching off drops every other pending choice. A source can
// only be chosen while the projector reports it is on (see _refresh), and a choice still waiting is dropped
// once it reports standby (see _settlePending); while the projector starts up, a later source does not wait for
// the earlier one's call (see _overtakes), nor does switching off (see _powerMayGo). Settings not listed here have
// a lane of their own.
const LANES = {
  auto_brightness: "brightness",
  auto_focus: "focus",
  auto_rotation: "rotation",
  picture_mode: "picture",
  picture_saturation: "picture",
  picture_brightness: "picture",
  picture_contrast: "picture",
  picture_sharpness: "picture",
  white_balance_red: "picture",
  white_balance_green: "picture",
  white_balance_blue: "picture",
};

// Side margin of the card. The source chips scroll from edge to edge; their fades live in this margin.
const EDGE = 16;

// Mask for the source chips: smoothstep fades of --fl and --fr on the left and right.
const FADE_MASK = (() => {
  const steps = [0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => i / 8);
  const alpha = (t) => (t * t * (3 - 2 * t)).toFixed(3);
  const left = steps.map((t) => `rgba(0,0,0,${alpha(t)}) calc(var(--fl) * ${t})`);
  const right = steps.map((t) => `rgba(0,0,0,${alpha(1 - t)}) calc(100% - var(--fr) * ${1 - t})`);
  return `linear-gradient(to right, ${[...left, ...right].join(", ")})`;
})();

const STYLE = `
  :host { display: block; }
  ha-card { padding: 4px 0 8px; overflow: hidden; }
  .header { display: flex; align-items: center; gap: 12px; padding: 8px 8px 8px 16px; }
  .icon { color: var(--state-icon-color, var(--paper-item-icon-color)); flex: none; }
  .icon.on { color: var(--primary-color); }
  /* The header's layout depends only on the card's width, never on the state or its wording: the state word
     ("On", "Standby") always shows, on its own line under the name (as in the dialog's bar), so a state change
     never moves the name, and the battery chip keeps its place and width (see .level). Each line has a fixed
     height and is cut with an ellipsis. */
  .titles { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .name { font-size: 16px; line-height: 20px; font-weight: 500; color: var(--primary-text-color); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .state { height: 16px; font-size: 13px; line-height: 16px; color: var(--secondary-text-color); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  /* A narrow card (a half-width card in a sections view is about 150 to 250 px wide) would leave the name little
     room: under 240 px the battery shows only its icon (the percentage is a tap away), under 200 px the projector
     icon goes and under 170 px the spacing tightens. */
  ha-card { container: lspx-card / inline-size; }
  @container lspx-card (width < 240px) { .header:not(.bar) .battery-chip .level { display: none; } }
  @container lspx-card (width < 200px) { .header:not(.bar) .icon { display: none; } }
  @container lspx-card (width < 170px) { .header:not(.bar) { gap: 8px; padding-left: 12px; } }
  .section { padding: 8px 16px; border-top: 1px solid var(--divider-color); }
  .section:first-of-type { border-top: none; }
  .section-head { display: flex; align-items: center; justify-content: space-between; min-height: 32px; }
  .title { color: var(--primary-color); font-weight: 500; font-size: 15px; }
  .row { display: flex; align-items: center; gap: 4px; min-height: 44px; }
  .row input[type=range] { flex: 1; min-width: 60px; }
  .value { min-width: 40px; text-align: right; font-variant-numeric: tabular-nums; color: var(--secondary-text-color); font-size: 13px; }
  input[type=range] { -webkit-appearance: none; appearance: none; height: 28px; margin: 0 4px; cursor: pointer; background: transparent; --pct: 0%;
    --track: color-mix(in srgb, var(--primary-text-color) 16%, transparent); }
  input[type=range]:disabled { cursor: default; opacity: 0.45; }
  input[type=range]:focus-visible { outline: none; }
  input[type=range]::-webkit-slider-runnable-track { height: 4px; border-radius: 2px;
    background: linear-gradient(to right, var(--primary-color) var(--pct), var(--track) var(--pct)); }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 16px; height: 16px; margin-top: -6px;
    border-radius: 50%; border: none; background: var(--primary-color); }
  input[type=range]:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 4px color-mix(in srgb, var(--primary-color) 30%, transparent); }
  input[type=range]::-moz-range-track { height: 4px; border-radius: 2px; background: var(--track); }
  input[type=range]::-moz-range-progress { height: 4px; border-radius: 2px; background: var(--primary-color); }
  input[type=range]::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%; border: none; background: var(--primary-color); }
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
  .turned { transform: rotate(180deg); }
  .keystone-zero { position: relative; flex: 1; display: flex; }
  .keystone-zero::before { content: ""; position: absolute; left: 50%; top: -2px; width: 4px; height: 4px; margin-left: -2px; border-radius: 50%; background: var(--secondary-text-color); }
  .keystone-zero input { flex: 1; }
  .sliders .row { min-height: 40px; }
  .sliders ha-icon { color: var(--secondary-text-color); margin-right: 8px; }
  .sliders .label { width: 150px; flex: none; color: var(--primary-text-color); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .switch-row { display: flex; align-items: center; justify-content: space-between; min-height: 44px; }
  .dim { opacity: 0.55; }
  @keyframes pending { 50% { opacity: 0.4; } }
  .pending { animation: pending 1.2s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) { .pending { animation: none; opacity: 0.6; } }
  .battery-chip { display: inline-flex; align-items: center; gap: 2px; color: var(--secondary-text-color); font-size: 13px; cursor: pointer;
    border-radius: 16px; padding: 4px 6px; font-variant-numeric: tabular-nums; }
  .battery-chip:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
  .battery-chip ha-icon { --mdc-icon-size: 18px; }
  .battery-chip:empty { display: none; }
  /* The percentage takes the room of "100%" whatever it says, so the chip keeps its width as the level changes. */
  .battery-chip .level { display: inline-grid; justify-items: start; }
  .battery-chip .level::after { content: "100%"; grid-area: 1 / 1; visibility: hidden; }
  .battery-chip .level > span { grid-area: 1 / 1; }
  /* Sources sit right under the header, with no divider. */
  .section.sources { border-top: none; padding-top: 0; }
  .compact-row { display: flex; align-items: center; gap: 4px; min-height: 44px; padding: 0 8px 4px; }
  .compact-row input[type=range] { flex: 1; min-width: 60px; }
  .compact-sources { padding-bottom: 2px; }
  .section.sources .chips { margin: 0 -16px; }
  /* The strip runs from card edge to card edge. The spacers line the first and last chips up with the
     header's icon and power button; the fades only cover those side margins. */
  .chips::before, .chips::after { content: ""; flex: 0 0 10px; }
  .chips { flex: 1; min-width: 0; display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; padding: 4px 0; overscroll-behavior-x: contain;
    /* Eased fades: a linear ramp shows a visible band where it meets full opacity. */
    --fl: 0px; --fr: 0px; -webkit-mask-image: ${FADE_MASK}; mask-image: ${FADE_MASK}; }
  .chips::-webkit-scrollbar { display: none; }
  .chips button { flex: none; width: auto; height: 32px; border-radius: 16px; padding: 0 12px; border: 1px solid var(--divider-color);
    white-space: nowrap; font-size: 13px; color: var(--primary-text-color); }
  .chips button.selected { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .warning { padding: 16px; color: var(--error-color); }
  .info { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; cursor: pointer; border-radius: 12px; margin: -4px; padding: 4px; }
  .info:hover { background: color-mix(in srgb, var(--primary-text-color) 5%, transparent); }
  .info:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 0; }
  /* Sizes use % of the viewport (the dialog is in the top layer), not vh, which on iOS includes the area
     under the browser toolbars. Home Assistant draws under the iOS status bar and home indicator, hence the
     safe-area insets. */
  dialog { box-sizing: border-box; border: none; padding: 0; width: min(480px, calc(100% - 32px)); max-width: none;
    max-height: calc(100% - 48px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); overflow: hidden;
    border-radius: var(--ha-dialog-border-radius, 24px); background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35); }
  dialog[open] { display: flex; flex-direction: column; }
  dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
  .dialog-content { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
  @media (max-width: 450px) {
    dialog { width: 100%; height: 100%; max-height: none; margin: 0; border-radius: 0;
      padding: env(safe-area-inset-top, 0px) env(safe-area-inset-right, 0px) 0 env(safe-area-inset-left, 0px); }
    .dialog-content { padding-bottom: env(safe-area-inset-bottom, 0px); }
  }
  /* In the dialog, the card's first line is the dialog's bar and stays put while the controls scroll under
     it (the embedded card's overflow is visible, as a card's hidden would make it the scroll container that
     sticky sticks to). */
  .header.bar { position: sticky; top: 0; z-index: 1; gap: 0; padding: 12px 8px 4px;
    background: var(--ha-card-background, var(--card-background-color, #fff)); }
  .header.bar .info { margin: -4px 4px; }
  .header.bar .name { font-size: 20px; line-height: 24px; }
  .header.bar button { width: 44px; height: 44px; }
  @media (max-width: 360px) {
    .header.bar button { width: 40px; }
    .header.bar .name { font-size: 18px; }
  }
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
    if (this._dialogCard) this._dialogCard.hass = hass;
  }

  getCardSize() {
    return { compact: 3, standard: 7, full: 12 }[this._config?.layout] ?? 7;
  }

  getGridOptions() {
    const rows = { compact: 3, standard: 7, full: 12 }[this._config?.layout] ?? 7;
    return { columns: 12, min_columns: 6, rows, min_rows: rows };
  }

  // --- entity lookup -------------------------------------------------------

  _t(word) {
    const lang = this._hass?.locale?.language || this._hass?.language || "en";
    const table = WORDS[lang] || WORDS[lang.split("-")[0]] || WORDS.en;
    return table[word] || WORDS.en[word];
  }

  /**
   * A source's name in the dashboard's language. Home Assistant cannot translate a media player's
   * sources, so the card does it for its own chips; the value sent stays the English name.
   */
  _sourceLabel(source) {
    const lang = this._hass?.locale?.language || this._hass?.language || "en";
    const posters = (WORDS[lang] || WORDS[lang.split("-")[0]])?.posters;
    if (!posters) return source;
    const clock = /^Clock (\d+)$/.exec(source);
    if (clock) return posters.clock.replace("{n}", clock[1]);
    return posters[source] || source;
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
    // With the entities and hass of the card that opened the dialog: the dialog's card gets no new ones once the
    // dialog closes, and a choice made in it may go out later (see _pending).
    if (this._owner) return this._owner._call(domain, service, key, data);
    const entity_id = this._ids[key];
    if (!entity_id) return Promise.resolve();
    // notifyOnError false: the card reports errors itself (_toast), but not those of choices replaced meanwhile;
    // Home Assistant's own toast would come on top.
    return this._hass.callService(domain, service, { entity_id, ...data }, undefined, false);
  }

  // --- optimistic updates --------------------------------------------------

  // The card in the details dialog shares its card's choices and lanes, so a source chosen on the card shows as
  // chosen in the dialog too (and the other way round), and the two never send related changes at once. The card
  // that opened the dialog runs them (_act, _settlePending): it stays connected, with the latest hass, once the
  // dialog closes.
  get _pending() {
    if (this._owner) return this._owner._pending;
    if (!this.__pending) this.__pending = {};
    return this.__pending;
  }

  get _busyLanes() {
    if (this._owner) return this._owner._busyLanes;
    if (!this.__busyLanes) this.__busyLanes = new Map(); // lane -> calls in flight
    return this.__busyLanes;
  }

  /** Redraw this card and the one sharing its choices (the dialog's, or the card that opened it). */
  _refreshAll() {
    this._refresh();
    (this._owner || this._dialogCard)?._refresh();
  }

  /**
   * Show `value` for setting `key` at once and send it with `send(value)`, keeping it until the projector
   * agrees. Changes go out through lanes (see LANES): one call per lane at a time, and a setting changed
   * again before its turn keeps only its latest value, so a burst of clicks or a drag sends the first
   * value and then the last, at the pace the projector manages.
   */
  _act(key, value, send, holdMs = HOLD_MS) {
    if (this._owner) return this._owner._act(key, value, send, holdMs);
    let entry = this._pending[key];
    if (!entry) {
      entry = { lane: LANES[key] || key };
      entry.pulseTimer = setTimeout(() => {
        entry.pulsing = true;
        this._refreshAll();
      }, PULSE_AFTER_MS);
      this._pending[key] = entry;
    }
    clearTimeout(entry.holdTimer);
    Object.assign(entry, { value, send, holdMs, changedAt: performance.now() });
    if (key === "power" && value === false) {
      // In standby nothing else is shown as chosen: drop every other pending choice, sent or not (a call in
      // flight still finishes; its lane is freed when it returns).
      for (const [k, e] of Object.entries(this._pending)) {
        if (e.lane !== "power") this._drop(k, e);
      }
    }
    this._refreshAll();
    this._pump(entry.lane);
  }

  /** A power change is waiting or in flight: other lanes hold their changes until it is done. */
  _powerFirst() {
    const power = this._pending.power;
    return this._busyLanes.has("power") || (!!power && !("sent" in power && power.sent === power.value));
  }

  /**
   * Whether a change of `lane` goes out while an earlier call of the lane is still in flight. Only a source chosen
   * while the main board starts up: that call is answered only once the board is up (up to two minutes), and the
   * integration sends only the latest choice, answering an earlier one it has not sent, without error, once the
   * start-up is over.
   */
  _overtakes(lane) {
    return lane === "source" && this._startingUp();
  }

  /**
   * Whether the power change waiting may go out now: once calls in flight are done. Switching off while the main
   * board starts up does not wait for source calls: one still waiting for the board is answered without error once
   * the projector is in standby, and a switch already under way finishes first in the integration, which keeps the
   * order. Switching off drops the source choice on the card at once (see _act).
   */
  _powerMayGo() {
    const busy = this._busyLanes;
    if (this._pending.power?.value === false && this._startingUp()) return [...busy.keys()].every((l) => l === "source");
    return busy.size === 0;
  }

  _startingUp() {
    return this._state("projector")?.attributes.starting_up === true;
  }

  /** Send the next waiting change of `lane`, if the lane (and power) is free. */
  _pump(lane) {
    const busy = this._busyLanes;
    if (busy.has(lane) && !this._overtakes(lane)) return;
    // Power goes out once calls in flight are done (see _powerMayGo); the other lanes wait for power.
    if (lane === "power" ? !this._powerMayGo() : this._powerFirst()) return;
    const waiting = Object.entries(this._pending)
      .filter(([, e]) => e.lane === lane && !("sent" in e && e.sent === e.value))
      .sort(([, a], [, b]) => a.changedAt - b.changedAt);
    if (!waiting.length) return;
    const [key, entry] = waiting[0];
    busy.set(lane, (busy.get(lane) || 0) + 1);
    const sent = (entry.sent = entry.value);
    // The entry's latest call: an earlier one, overtaken, neither starts the hold nor drops the entry, nor reports
    // an error: the choice it sent was replaced, and the latest call reports how that one went.
    const call = (entry.call = {});
    clearTimeout(entry.holdTimer);
    const done = () => {
      const left = busy.get(lane) - 1;
      if (left) busy.set(lane, left);
      else busy.delete(lane);
      if (lane === "power") new Set(Object.values(this._pending).map((e) => e.lane)).forEach((l) => this._pump(l));
      else {
        this._pump("power");
        this._pump(lane);
      }
    };
    entry.send(sent).then(
      () => {
        if (this._pending[key] === entry && entry.call === call && entry.value === sent) {
          entry.holdTimer = setTimeout(() => this._drop(key, entry), entry.holdMs);
        }
        done();
      },
      (err) => {
        if (entry.call === call) {
          this._drop(key, entry);
          this._toast(err);
        }
        done();
      },
    );
  }

  _drop(key, entry) {
    if (this._pending[key] !== entry) return;
    this._dropTimers(entry);
    delete this._pending[key];
    this._refreshAll();
  }

  _dropTimers(entry) {
    if (!entry) return;
    clearTimeout(entry.pulseTimer);
    clearTimeout(entry.holdTimer);
  }

  /** The value of control `key` as the projector reports it. */
  _actual(key) {
    const projector = this._state("projector");
    const on = projector?.state === "on";
    if (key === "power") return on;
    if (key === "source") return on ? projector.attributes.source : null;
    if (key === "volume") {
      const level = projector?.attributes.volume_level;
      return level == null ? null : Math.round(level * 100);
    }
    const s = this._state(key);
    if (!s) return undefined;
    if (s.entity_id.startsWith("switch.")) return s.state === "on";
    if (s.entity_id.startsWith("number.")) return Number(s.state);
    return s.state;
  }

  /** The value to show: the pending choice while there is one, otherwise the projector's. */
  _shown(key) {
    // With standby chosen, nothing shows as the current source.
    if (key === "source" && !this._shown("power")) return null;
    return key in this._pending ? this._pending[key].value : this._actual(key);
  }

  /** For buttons: a button never pressed is "unknown", which is fine; only "unavailable" is not. */
  _usable(key) {
    const s = this._state(key);
    return !!s && s.state !== "unavailable";
  }

  _isPulsing(key) {
    return !!this._pending[key]?.pulsing;
  }

  _settlePending() {
    for (const [key, entry] of Object.entries(this._pending)) {
      // A pending choice ends as soon as the projector reports it. A source choice also ends once the
      // projector reports standby: sent then, it would switch the projector on (a call in flight still
      // finishes; its lane is freed when it returns).
      if (this._actual(key) === entry.value || (key === "source" && this._state("projector")?.state === "off")) {
        this._dropTimers(entry);
        delete this._pending[key];
      }
    }
  }

  async _toast(err) {
    const message = (await this._translatedError(err)) || err?.message || String(err);
    // From the dialog's card only while the dialog is open: closed, its events reach nothing.
    const target = this.isConnected ? this : this._owner || this;
    target.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
  }

  /** The error in the user's language, as Home Assistant's own toast shows it; nothing if it has no translation. */
  async _translatedError(err) {
    const hass = (this._owner || this)._hass;
    const { translation_domain: domain, translation_key: key, translation_placeholders: placeholders } = err || {};
    if (!domain || !key || !hass?.loadBackendTranslation) return undefined;
    try {
      const localize = await hass.loadBackendTranslation("exceptions", domain);
      return localize(`component.${domain}.exceptions.${key}.message`, placeholders);
    } catch {
      return undefined;
    }
  }

  // --- rendering -----------------------------------------------------------

  _update() {
    if (!this._config || !this._hass) return;
    this._ids = this._findEntities();
    // Rebuild only when the structure changes, as a rebuild resets scrolling and interaction. Besides the
    // layout, language and entities, that is when the media player's state appears (it may still be missing
    // while Home Assistant starts) and when its source list (the chips) appears or changes: a restored or
    // unavailable state can lack it. Pending choices live on the card, so they survive a rebuild.
    const projector = this._hass.states[this._config.entity];
    const key = JSON.stringify([
      this._config.layout,
      this._hass.locale?.language || this._hass.language,
      Object.values(this._ids),
      !!projector,
      projector?.attributes.source_list ?? [],
    ]);
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
    // The new content replaces the old, including an open details dialog, which goes without a close event.
    this._releasePage();
    this._stripObserver?.disconnect();
    this._dialogCard?.remove();
    this._dialogCard = null;
    this._dialog = null;
    if (!this._hass.states[this._config.entity]) {
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card><div class="warning"></div></ha-card>`;
      this.shadowRoot.querySelector(".warning").textContent = `Entity not found: ${this._config.entity}`;
      return;
    }
    const embedded = !!this._config._embedded;
    // In the dialog the name opens Home Assistant's own entity dialog (the card there is "full").
    const info = `
          <div class="info" data-action="details" role="button" tabindex="0">
            ${embedded ? "" : `<ha-icon class="icon" data-ref="icon" icon="mdi:projector"></ha-icon>`}
            <div class="titles"><div class="name" data-ref="name"></div><span class="state" data-ref="state"></span></div>
          </div>`;
    const batteryChip = `<span class="battery-chip" data-ref="battery" role="button" tabindex="0"></span>`;
    const power = `<button class="power" data-action="power"><ha-icon icon="mdi:power"></ha-icon></button>`;
    const sources = this._state("projector")?.attributes.source_list || [];
    const chips = `<div class="chips" data-ref="source-chips" role="listbox" aria-label="${this._t("source")}">${sources
      .map((x) => `<button data-source="${x}" role="option">${this._sourceLabel(x)}</button>`)
      .join("")}</div>`;
    if (embedded) {
      const localize = (key, fallback) => this._hass.localize?.(key) || fallback;
      const barButton = (action, icon, key, fallback) =>
        `<button data-dialog="${action}" title="${localize(key, fallback)}" aria-label="${localize(key, fallback)}"><ha-icon icon="${icon}"></ha-icon></button>`;
      parts.push(`<div class="header bar">
        ${barButton("close", "mdi:close", "ui.common.close", "Close")}
        ${info}${batteryChip}${power}
        ${barButton("history", "mdi:chart-box-outline", "ui.dialogs.more_info_control.history", "History")}
        ${barButton("settings", "mdi:cog-outline", "ui.dialogs.more_info_control.settings", "Settings")}
      </div>`);
    } else parts.push(`<div class="header">${info}${batteryChip}${power}</div>`);
    if (layout === "compact") {
      if (sources.length) parts.push(`<div class="compact-sources">${chips}</div>`);
      parts.push(`
        <div class="compact-row">
          <button data-step="volume" data-dir="-1" title="${this._t("quieter")}"><ha-icon data-ref="volume-down" icon="mdi:volume-medium"></ha-icon></button>
          <input type="range" data-slider="volume" aria-label="${this._t("volume")}">
          <button data-step="volume" data-dir="1" title="${this._t("louder")}"><ha-icon icon="mdi:volume-high"></ha-icon></button>
        </div>`);
    } else {
      if (sources.length) parts.push(`<div class="section sources">${chips}</div>`);
      // xLOUD fills the Volume header's free corner, where other sections have their Auto checkbox.
      parts.push(
        this._sliderSection("volume", this._t("volume"), "mdi:minus-circle-outline", "mdi:plus-circle-outline", "xloud", null, "xLOUD"),
      );
      parts.push(this._sliderSection("brightness", null, "mdi:minus-circle-outline", "mdi:plus-circle-outline", "auto_brightness"));
      parts.push(this._sliderSection("focus", null, "mdi:chevron-left", "mdi:chevron-right", "auto_focus"));
      parts.push(this._sliderSection("keystone", null, "mdi:chevron-left", "mdi:chevron-right", null, "keystone_reset"));
      if (this._ids.rotation) {
        parts.push(`
          <div class="section">
            <div class="section-head"><span class="title" data-label="rotation"></span>${this._autoBox("auto_rotation")}</div>
            <div class="segments" data-segments="rotation">
              ${ROTATIONS.map(([o, icon, cls]) => `<button data-option="${o}"><ha-icon icon="${icon}"${cls ? ` class="${cls}"` : ""}></ha-icon></button>`).join("")}
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
        // Like the app: resetting the custom picture settings comes last.
        if (this._ids.picture_reset) {
          parts.push(`
            <div class="section" data-ref="picture-reset-section"><div class="switch-row">
              <span class="title" data-label="picture_reset"></span>
              <button class="text" data-action="picture_reset">${this._t("reset")}</button></div></div>`);
        }
      }
    }
    const embeddedStyle = embedded
      ? `<style>ha-card { box-shadow: none; border: none; border-radius: 0; background: none; padding-top: 0; overflow: visible; }</style>`
      : "";
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>${embeddedStyle}<ha-card>${parts.join("")}</ha-card>`;
    this._bind();
  }

  _autoBox(key, label = this._t("auto")) {
    if (!this._ids[key]) return "";
    return `<label class="auto">${label}<input type="checkbox" data-toggle="${key}"></label>`;
  }

  _sliderSection(key, title, minusIcon, plusIcon, autoKey = null, resetKey = null, autoLabel = undefined) {
    if (key !== "volume" && !this._ids[key]) return "";
    const right = autoKey
      ? this._autoBox(autoKey, autoLabel)
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
    root.querySelectorAll("[data-action=details]").forEach((el) => {
      el.addEventListener("click", () => this._openDetails());
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          this._openDetails();
        }
      });
    });
    // The dialog's bar (see _openDetails): closing is up to the dialog; the other views are Home Assistant's.
    root.querySelectorAll("[data-dialog]").forEach((b) =>
      b.addEventListener("click", () => {
        if (b.dataset.dialog === "close") this.dispatchEvent(new CustomEvent(CLOSE_EVENT, { bubbles: true }));
        else this._moreInfo(b.dataset.dialog);
      }),
    );
    const battery = root.querySelector("[data-ref=battery]");
    const openBattery = () => {
      if (!this._ids.battery) return;
      this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: this._ids.battery }, bubbles: true, composed: true }));
    };
    battery?.addEventListener("click", openBattery);
    battery?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openBattery();
      }
    });
    const strip = root.querySelector("[data-ref=source-chips]");
    if (strip) {
      strip.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () =>
          this._act(
            "source",
            b.dataset.source,
            (source) => this._call("media_player", "select_source", "projector", { source }),
            SOURCE_HOLD_MS,
          ),
        ),
      );
      strip.addEventListener("scroll", () => updateFades(strip), { passive: true });
      // Once the user scrolls the strip, leave it where they put it until the source changes.
      for (const type of ["pointerdown", "touchstart", "wheel"]) {
        strip.addEventListener(type, () => (strip._touched = true), { passive: true });
      }
      // A mouse wheel scrolls the strip sideways.
      strip.addEventListener(
        "wheel",
        (e) => {
          if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || strip.scrollWidth <= strip.clientWidth) return;
          e.preventDefault();
          strip.scrollLeft += e.deltaY;
        },
        { passive: false },
      );
      // The first layout pass may not have the final width yet: centre again until the user scrolls.
      this._stripObserver = new ResizeObserver(() => {
        if (!strip._touched) centerSelected(strip, false);
        updateFades(strip);
      });
      this._stripObserver.observe(strip);
    }
    root.querySelectorAll("[data-action=power]").forEach((b) =>
      b.addEventListener("click", () => {
        this._act("power", !this._shown("power"), (on) =>
          this._call("media_player", on ? "turn_on" : "turn_off", "projector"),
        );
      }),
    );
    root.querySelector("[data-action=keystone_reset]")?.addEventListener("click", () =>
      this._act("keystone", 0, () => this._call("button", "press", "keystone_reset")),
    );
    root.querySelector("[data-action=picture_reset]")?.addEventListener("click", () =>
      this._call("button", "press", "picture_reset").catch((err) => this._toast(err)),
    );
    root.querySelectorAll("input[data-slider]").forEach((input) => {
      // Send values while dragging, not only on release; _act keeps it to one call at a time.
      input.addEventListener("input", () => {
        const v = root.querySelector(`[data-value="${input.dataset.slider}"]`);
        if (v) v.textContent = input.value;
        setFill(input);
        this._setValue(input.dataset.slider, Number(input.value));
      });
      // Never move the knob under a finger or mouse (touch does not focus the slider).
      input.addEventListener("pointerdown", () => (input._dragging = true));
      for (const type of ["pointerup", "pointercancel", "change"]) {
        input.addEventListener(type, () => (input._dragging = false));
      }
    });
    root.querySelectorAll("button[data-step]").forEach((b) =>
      b.addEventListener("click", () => {
        const key = b.dataset.step;
        const input = root.querySelector(`input[data-slider="${key}"]`);
        const step = { volume: 5, brightness: 5, focus: 5, keystone: 1 }[key] || 1;
        const next = Math.min(Number(input.max), Math.max(Number(input.min), Number(input.value) + step * Number(b.dataset.dir)));
        input.value = next;
        setFill(input);
        this._setValue(key, next);
      }),
    );
    root.querySelectorAll("input[data-toggle]").forEach((box) =>
      box.addEventListener("change", () =>
        this._act(box.dataset.toggle, box.checked, (on) =>
          this._call("switch", on ? "turn_on" : "turn_off", box.dataset.toggle),
        ),
      ),
    );
    root.querySelectorAll("[data-segments=rotation] button").forEach((b) =>
      b.addEventListener("click", () =>
        this._act("rotation", b.dataset.option, (option) =>
          this._call("select", "select_option", "rotation", { option }),
        ),
      ),
    );
  }

  // --- details dialog ------------------------------------------------------

  _moreInfo(view) {
    // Home Assistant's own entity dialog; view is "info", "history", "settings" or "related".
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { detail: { entityId: this._config.entity, view }, bubbles: true, composed: true }),
    );
  }

  _openDetails() {
    if (this._config.layout === "full") {
      this._moreInfo("info");
      return;
    }
    if (!this._dialog) {
      // The card inside draws the dialog's bar as its own first line (close, name, battery, power, history,
      // settings), so battery and power need no row of their own.
      const dialog = document.createElement("dialog");
      dialog.innerHTML = `<div class="dialog-content"></div>`;
      dialog.addEventListener(CLOSE_EVENT, () => dialog.close());
      // Close when clicking the backdrop (the dialog element itself, outside its content).
      dialog.addEventListener("click", (e) => {
        if (e.target === dialog) dialog.close();
      });
      // A vertical swipe with nothing to scroll in the dialog would scroll what is under it, whichever element that
      // is in the dashboard (the page itself is held by _holdPage; overscroll-behavior needs something to scroll).
      let start = null;
      dialog.addEventListener("touchstart", (e) => {
        start = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
      }, { passive: true });
      dialog.addEventListener("touchmove", (e) => {
        if (!start || !e.cancelable) return;
        const dx = Math.abs(e.touches[0].clientX - start.x);
        const dy = Math.abs(e.touches[0].clientY - start.y);
        const content = dialog.querySelector(".dialog-content");
        const scrolls = content.scrollHeight > content.clientHeight && e.composedPath().includes(content);
        if (dy > dx && !scrolls) e.preventDefault();
        else start = null; // a sideways swipe (the sources, a slider) or a scroll: the browser's from here on
      }, { passive: false });
      // The card inside opens Home Assistant's dialogs (entity, history, settings, battery): make way for them.
      dialog.addEventListener("hass-more-info", () => dialog.close());
      dialog.addEventListener("close", () => {
        this._releasePage();
        this._dialogCard?.remove();
        this._dialogCard = null;
      });
      this.shadowRoot.appendChild(dialog);
      this._dialog = dialog;
    }
    const card = document.createElement(CARD_TYPE);
    card._owner = this;
    card.setConfig({ ...this._config, layout: "full", _embedded: true });
    card.hass = this._hass;
    this._dialogCard?.remove();
    this._dialogCard = card;
    this._dialog.querySelector(".dialog-content").appendChild(card);
    this._dialog.setAttribute("aria-label", this._config.name || this._deviceName() || this._config.entity);
    this._dialog.showModal();
    this._holdPage();
    // showModal() does not look into the card's shadow root, where the bar's buttons are.
    card.shadowRoot?.querySelector("[data-dialog=close]")?.focus();
  }

  /** Keep the page under the open dialog from scrolling: a swipe on a dialog whose content fits would move it. */
  _holdPage() {
    if (this._releaseScroll) return;
    const root = document.documentElement;
    const before = [root.style.overflow, root.style.scrollbarGutter];
    // Where scrollbars take up space, keep it, so that the page does not shift.
    if (window.innerWidth > root.clientWidth) root.style.scrollbarGutter = "stable";
    root.style.overflow = "hidden";
    this._releaseScroll = () => ([root.style.overflow, root.style.scrollbarGutter] = before);
  }

  _releasePage() {
    this._releaseScroll?.();
    this._releaseScroll = null;
  }

  disconnectedCallback() {
    // Gone with its view, say (back while the dialog is open): the dialog goes without a close event.
    if (this._dialog?.open) this._dialog.close();
    this._releasePage();
  }

  _setValue(key, value) {
    this._act(key, value, (v) =>
      key === "volume"
        ? this._call("media_player", "volume_set", "projector", { volume_level: v / 100 })
        : this._call("number", "set_value", key, { value: v }),
    );
  }

  _refresh() {
    const root = this.shadowRoot;
    if (!root || !root.querySelector("ha-card")) return;
    const projector = this._state("projector");
    if (!projector) return;
    const reachable = projector.state !== "unavailable";
    if (!this._owner) this._settlePending();
    // Controls follow the power state the user chose: switching on enables them at once (their changes
    // wait for power, see _pump), switching off disables them at once. The source chips also wait until the
    // media player reports on, so a source is never chosen in standby and choosing one does not switch the
    // projector on; that is the power button's job. The media player reports on once the projector answers
    // the power command, while it may still be starting up; the integration retries commands through that.
    const on = reachable && this._shown("power");
    const sourcesOn = on && this._actual("power");
    // Switched on (or restarted) from a long standby, the projector's main board takes about a minute to start:
    // it reports on, shows no source yet, and a source chosen meanwhile is held by the integration and applied
    // once the board is up (the chip stays chosen and pulses until then).
    const starting = sourcesOn && projector.attributes.starting_up === true;

    // A word under the name always tells the state: On, Starting up, Standby or Unavailable. Like the controls,
    // it follows the power state the user chose. Any other state (unknown while Home Assistant starts) is Home
    // Assistant's own word for it.
    const stateEl = root.querySelector("[data-ref=state]");
    if (stateEl) {
      const chosenOff = "power" in this._pending || projector.state === "off";
      stateEl.textContent = !reachable
        ? this._t("unavailable")
        : this._shown("power")
          ? this._t(starting ? "starting" : "on")
          : chosenOff
            ? this._t("off")
            : this._hass.formatEntityState?.(projector) || projector.state;
    }
    const nameEl = root.querySelector("[data-ref=name]");
    if (nameEl) nameEl.textContent = this._config.name || this._deviceName() || projector.attributes.friendly_name;
    const chip = root.querySelector("[data-ref=battery]");
    if (chip) {
      // The chip stays while the battery is unavailable (as every entity is while polls fail), so the header
      // keeps its layout; it goes only when there is no battery entity.
      const battery = this._state("battery");
      const level = Number(battery?.state);
      const known = battery?.state !== "" && !Number.isNaN(level);
      if (battery) {
        chip.title = this._label("battery") || (known ? `${level}%` : "");
        const icon = known ? batteryIcon(level, this._state("charge_state")?.state) : "mdi:battery-unknown";
        chip.innerHTML = `<ha-icon icon="${icon}"></ha-icon><span class="level"><span>${known ? `${level}%` : "–"}</span></span>`;
      } else chip.innerHTML = "";
    }
    // At volume 0 the "quieter" button shows a muted speaker.
    const volumeDown = root.querySelector("[data-ref=volume-down]");
    if (volumeDown) volumeDown.icon = on && this._shown("volume") === 0 ? "mdi:volume-mute" : "mdi:volume-medium";
    const strip = root.querySelector("[data-ref=source-chips]");
    if (strip) {
      const current = this._shown("source");
      strip.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("selected", b.dataset.source === current);
        b.classList.toggle("pending", b.dataset.source === current && this._isPulsing("source"));
        b.setAttribute("aria-selected", String(b.dataset.source === current));
        b.disabled = !sourcesOn;
      });
      // Bring the selected source into view when it changes (not while the user scrolls around).
      if (strip.dataset.shown !== String(current)) {
        const first = strip.dataset.shown === undefined;
        strip.dataset.shown = String(current);
        strip._touched = false;
        requestAnimationFrame(() => {
          centerSelected(strip, !first);
          updateFades(strip);
        });
      }
    }
    const powerShown = this._shown("power");
    root.querySelector("[data-ref=icon]")?.classList.toggle("on", powerShown);
    root.querySelectorAll("[data-action=power]").forEach((b) => {
      b.classList.toggle("on", powerShown);
      b.classList.toggle("pending", this._isPulsing("power"));
      b.disabled = !reachable;
      b.title = !reachable
        ? this._t("unavailable")
        : starting
          ? this._t("starting")
          : on
            ? this._t("on")
            : this._t("off");
    });

    root.querySelectorAll("[data-label]").forEach((el) => (el.textContent = this._label(el.dataset.label)));

    // Sliders
    root.querySelectorAll("input[data-slider]").forEach((input) => {
      const key = input.dataset.slider;
      let value, min, max, enabled;
      if (key === "volume") {
        min = 0;
        max = 100;
        enabled = on;
      } else {
        const s = this._state(key);
        if (!s) return;
        min = s.attributes.min;
        max = s.attributes.max;
        enabled = on && this._available(key);
      }
      value = this._shown(key);
      input.classList.toggle("pending", this._isPulsing(key));
      input.min = min;
      input.max = max;
      input.step = 1;
      if (!input._dragging && this.shadowRoot.activeElement !== input && value != null && !Number.isNaN(value)) {
        input.value = value;
      }
      input.disabled = !enabled;
      setFill(input);
      const v = root.querySelector(`[data-value="${key}"]`);
      if (v) v.textContent = enabled && value != null && !Number.isNaN(value) ? value : "–";
      root.querySelectorAll(`button[data-step="${key}"]`).forEach((b) => (b.disabled = !enabled));
    });

    // Auto checkboxes and toggles; the manual slider is dimmed while Auto is on
    root.querySelectorAll("input[data-toggle]").forEach((box) => {
      const key = box.dataset.toggle;
      box.checked = this._shown(key) === true;
      box.closest("label")?.classList.toggle("pending", this._isPulsing(key));
      box.disabled = !on || !this._available(key);
    });
    for (const [slider, auto] of [["brightness", "auto_brightness"], ["focus", "auto_focus"]]) {
      const input = root.querySelector(`input[data-slider="${slider}"]`);
      input?.classList.toggle("dim", this._shown(auto) === true);
    }

    // Rotation
    const rotation = this._state("rotation");
    root.querySelectorAll("[data-segments=rotation] button").forEach((b) => {
      if (rotation) {
        const label = this._optionLabel(rotation, b.dataset.option);
        if (b.title !== label) {
          b.title = label;
          b.setAttribute("aria-label", label);
        }
      }
      const selected = this._shown("rotation") === b.dataset.option;
      b.classList.toggle("selected", selected);
      b.setAttribute("aria-pressed", String(selected));
      b.classList.toggle("pending", selected && this._isPulsing("rotation"));
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
          b.addEventListener("click", () =>
            this._act("picture_mode", b.dataset.option, (option) =>
              this._call("select", "select_option", "picture_mode", { option }),
            ),
          ),
        );
      }
      modeBox.querySelectorAll("button").forEach((b) => {
        const selected = this._shown("picture_mode") === b.dataset.option;
        b.classList.toggle("selected", selected);
        b.setAttribute("aria-pressed", String(selected));
        b.classList.toggle("pending", selected && this._isPulsing("picture_mode"));
        b.disabled = !on || !this._available("picture_mode");
      });
      const custom = String(this._shown("picture_mode")).startsWith("custom");
      const sliders = root.querySelector("[data-ref=picture-sliders]");
      if (sliders) sliders.style.display = custom ? "" : "none";
      const resetSection = root.querySelector("[data-ref=picture-reset-section]");
      if (resetSection) resetSection.style.display = custom ? "" : "none";
      const reset = root.querySelector("[data-action=picture_reset]");
      if (reset) reset.disabled = !on || !this._usable("picture_reset");
    }
    const keystoneReset = root.querySelector("[data-action=keystone_reset]");
    // Nothing to reset while the keystone is already 0.
    if (keystoneReset) keystoneReset.disabled = !on || !this._usable("keystone_reset") || this._shown("keystone") === 0;
  }

  _optionLabel(stateObj, option) {
    // Translated state names come from the integration's translation files.
    const translated = this._hass.formatEntityState?.(stateObj, option);
    if (translated && translated !== option) return translated;
    return option.replace(/_(\d)$/, " $1").replace(/^./, (c) => c.toUpperCase()).replace("_", " ");
  }
}

// A chip cut by the card edge must show past the fade and hide a little, so it reads as cut off.
const CUT_SHOWN = EDGE + 8;
const CUT_HIDDEN = 8;

function centerSelected(strip, smooth) {
  // Show the selected chip in full, as close to the middle as possible, and where chips are hidden
  // make the card edge cut well into a chip, so the strip never looks like a complete list.
  const chip = strip.querySelector("button.selected");
  const width = strip.clientWidth;
  const max = strip.scrollWidth - width;
  if (!chip || !width || max <= 0) return;
  const box = strip.getBoundingClientRect();
  const at = (el) => {
    const r = el.getBoundingClientRect();
    return [r.left - box.left + strip.scrollLeft, r.right - box.left + strip.scrollLeft];
  };
  const spans = [...strip.querySelectorAll("button")].map(at);
  const [start, end] = at(chip);
  const fits = (left) => {
    if (start < left + EDGE || end > left + width - EDGE) return false;
    const l = left;
    const r = left + width;
    if (l > 0.5 && !spans.some(([a, b]) => a <= l - CUT_HIDDEN && b >= l + CUT_SHOWN)) return false;
    if (r < max + width - 0.5 && !spans.some(([a, b]) => b >= r + CUT_HIDDEN && a <= r - CUT_SHOWN)) return false;
    return true;
  };
  const ideal = (start + end - width) / 2;
  let best = null;
  for (let d = 0; d <= width / 2 && best === null; d += 2) {
    for (const candidate of [ideal + d, ideal - d]) {
      // Whole pixels: the browser rounds scrollLeft, which could undo a cut that only just fits.
      const left = Math.round(Math.min(max, Math.max(0, candidate)));
      if (fits(left)) {
        best = left;
        break;
      }
    }
  }
  strip.scrollTo({ left: best ?? Math.min(max, Math.max(0, ideal)), behavior: smooth ? "smooth" : "instant" });
}

function updateFades(strip) {
  // Fade the edges that hide more chips, so the strip reads as scrollable.
  const max = strip.scrollWidth - strip.clientWidth;
  strip.style.setProperty("--fl", strip.scrollLeft > 1 ? `${EDGE}px` : "0px");
  strip.style.setProperty("--fr", strip.scrollLeft < max - 1 ? `${EDGE}px` : "0px");
}

function setFill(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--pct", `${Math.max(0, Math.min(100, pct))}%`);
}

function batteryIcon(level, chargeState) {
  const tens = Math.max(0, Math.min(100, Math.round(level / 10) * 10));
  if (chargeState === "charging" || chargeState === "full") {
    return tens === 0 ? "mdi:battery-charging-outline" : `mdi:battery-charging-${tens}`;
  }
  if (tens === 100) return "mdi:battery";
  return tens === 0 ? "mdi:battery-outline" : `mdi:battery-${tens}`;
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
      description: "Projector controls laid out like Sony's app: source, volume, brightness, focus, keystone and screen rotation.",
      preview: true,
      documentationURL: "https://github.com/kachkaev/ha-sony-lspx-p1",
    });
  }
}

registerCard();
