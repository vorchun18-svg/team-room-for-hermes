/**
 * attach-preview-fe.test.mjs — «Просмотр» вложения (W29B-Q3).
 *
 * Проверяем выбор вида файла на клиенте: он решает, показывать ли кнопку
 * «Просмотр», и должен совпадать с сервером (`attach_preview.kind_of`), иначе
 * у файла появится кнопка-тупик или пропадёт нужная.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { kindOf, canPreview } from '../web/js/views/attach-preview.js';

test('вид файла по расширению', () => {
  assert.equal(kindOf('C:/материалы/чек.PNG'), 'image');
  assert.equal(kindOf('договор.pdf'), 'pdf');
  assert.equal(kindOf('план.md'), 'text');
  assert.equal(kindOf('заказы.csv'), 'table');
  assert.equal(kindOf('акт.docx'), 'docx');
  assert.equal(kindOf('смена.xlsx'), 'xlsx');
  assert.equal(kindOf('меню.pptx'), 'pptx');
  assert.equal(kindOf('голос.ogg'), 'audio');
  assert.equal(kindOf('запись.M4A'), 'audio');
  assert.equal(kindOf('архив.zip'), 'other');
  assert.equal(kindOf(''), 'other');
});

test('кнопка «Просмотр» есть у всех понятных типов и нет у неизвестных', () => {
  for (const path of ['a.png', 'a.pdf', 'a.txt', 'a.csv', 'a.docx', 'a.xlsx',
    'a.pptx', 'a.ogg', 'a.mp3', 'a.wav']) {
    assert.equal(canPreview(path), true, path);
  }
  assert.equal(canPreview('setup.exe'), false);
  assert.equal(canPreview(''), false);
});
