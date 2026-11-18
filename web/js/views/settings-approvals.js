/**
 * settings-approvals.js — раздел «Разрешения агентов» в настройках (W22-AP2).
 *
 * Список выданных разрешений участникам комнаты (`GET /api/approvals/grants`)
 * и кнопка «Отозвать» (`POST /api/approvals/revoke`). Живёт отдельным файлом,
 * как «Диктовка»: settings.js подключает раздел, если файл на месте, — экран
 * настроек не разрастается и не конфликтует с другими пакетами.
 *
 * Технику (ключи `command_allowlist`, профили) наружу не показываем: для
 * человека это «кто», «что разрешено», «до каких пор» и «откуда взялось».
 */

import { h } from '../util.js';
import { sectionHeader } from '../ui.js';
import { APPROVALS_RU } from '../text-ru.js';
import { createGrantsSection } from './approvals.js';

/**
 * Показать раздел в готовом контейнере.
 * @param {HTMLElement} host контейнер `.set-section`
 * @param {object} ctx контекст приложения {api, toast, room, route, store}
 * @returns {Promise<{destroy:Function, load:Function}>}
 */
export async function mountApprovalsSettings(host, ctx = {}) {
  if (!host) return { destroy() {}, load() {} };
  const api = ctx.api || null;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const roomsStore = (ctx.store && ctx.store.rooms) || null;
  // Комната — из адреса или первая в списке: разрешения читаются по участникам
  // комнаты, а настройки открываются и без комнаты в адресе.
  const roomId = String((ctx.route && ctx.route.roomId)
    || (ctx.room && ctx.room.id)
    || (((roomsStore && roomsStore.get && roomsStore.get('list')) || [])[0] || {}).id
    || '');

  host.replaceChildren();
  host.appendChild(sectionHeader(APPROVALS_RU.settingsTitle));
  host.appendChild(h('p', { class: 'set-group__hint small muted' }, APPROVALS_RU.settingsHint));

  const ui = createGrantsSection({ api, roomId, toast });
  host.appendChild(ui.element);

  return {
    load: () => ui.load(),
    destroy() {
      ui.destroy();
      host.replaceChildren();
    },
  };
}

export default mountApprovalsSettings;
