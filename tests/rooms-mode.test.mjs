/**
 * rooms-mode.test.mjs — «проектный режим» в карточке комнаты (Д2 WAVE8-RULES).
 *
 * Михаил видит в обзоре комнат пометку «проектный режим» только у комнат с
 * `project_mode === true`; старые ответы API без этого поля карточку не ломают.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom();

const HERE = dirname(fileURLToPath(import.meta.url));
const { isProjectMode, normalizeRoom } = await import('../web/js/views/rooms.js');
const src = readFileSync(join(HERE, '..', 'web', 'js', 'views', 'rooms.js'), 'utf8');

test('проектный режим: только при project_mode === true, без поля — false', () => {
  assert.equal(isProjectMode({ id: 'cafe', project_mode: true }), true);
  assert.equal(isProjectMode({ id: 'cafe', project_mode: false }), false);
  assert.equal(isProjectMode({ id: 'cafe' }), false, 'поле необязательное');
  assert.equal(isProjectMode(null), false, 'нет комнаты — не падаем');
  assert.equal(isProjectMode(undefined), false);
  assert.equal(isProjectMode({ project_mode: 'true' }), false, 'строка — не булево');
});

test('нормализация комнаты сохраняет project_mode и project_dir', () => {
  const room = normalizeRoom({ id: 'cafe', title: 'Лето', project_mode: true,
    project_dir: 'C:\\TeamRoom\\Projects\\Лето' });
  assert.equal(room.project_mode, true);
  assert.equal(room.project_dir, 'C:\\TeamRoom\\Projects\\Лето');
  assert.equal(normalizeRoom({ id: 'fn' }).project_mode, undefined, 'нет поля — не выдумываем');
});

test('бейдж «проектный режим» стоит в карточке и объясняет, что это', () => {
  assert.match(src, /rooms-card__mode/, 'класс бейджа');
  assert.match(src, /проектный режим/);
  assert.match(src, /if \(isProjectMode\(room\)\)/, 'показ по флагу');
  assert.match(src, /title: 'Проектный режим: /, 'подсказка при наведении');
  assert.doesNotMatch(src, /директор/i, 'старое слово не вернулось');
});

test('кнопки комнаты в шапке: папка и настройки на всех вкладках (W10)', () => {
  const app = readFileSync(join(HERE, '..', 'web', 'js', 'app.js'), 'utf8');
  assert.match(app, /import \{ folderActionButton, roomSettingsButton, projectDirHint \} from '\.\/project-dir-button\.js'/);
  assert.match(app, /right\.push\(roomSettingsButton\(roomCtx, room\)\)/,
    'постоянная кнопка «Настройки комнаты»');
  assert.match(app, /right\.push\(folderActionButton\(roomCtx, room\)\)/,
    'кнопка папки: «Открыть» или «Задать»');
  assert.match(app, /room && room\.project_dir/, 'смена папки перерисовывает шапку');
  // Подсказка «Задайте папку проекта…» живёт в полосе под вкладками, не в шапке.
  assert.match(app, /hintNode = projectDirHint\(roomCtx, \{ roomId: room\.id \}\)/);
});
