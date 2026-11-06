/**
 * Плагин «Командные комнаты» для Hermes Desktop.
 *
 * Показывает локальный сервер «Комната команды» (http://127.0.0.1:8777)
 * прямо внутри приложения — отдельной страницей на всю область, без рамок
 * и без вкладок браузера. Плюс строка в левом меню и команда в ⌘K / Ctrl+K.
 *
 * Правила живучести (Hermes обновляется часто и подменяет SDK):
 *  · импортируем ПРОСТРАНСТВА ИМЁН (import * as), а не именованные экспорты —
 *    исчезнувший именованный экспорт ломает загрузку всего файла на этапе
 *    связывания модуля, а отсутствующее свойство объекта — нет;
 *  · именами областей (routes / sidebar.nav / palette) берём из SDK, а если
 *    константа пропала — подставляем литерал;
 *  · весь register() в try/catch, всё необязательное — через ?. и try/catch;
 *  · собственный PLUGIN_VERSION — сторож сравнивает его с установленной копией.
 *
 * Только эти импорты: @hermes/plugin-sdk, react, react/jsx-runtime. Без JSX.
 */

import * as SDK from '@hermes/plugin-sdk'
import * as ReactNS from 'react'
import * as JSXNS from 'react/jsx-runtime'

export const PLUGIN_VERSION = '1.0.4'

const ID = 'team-room'
const NAME = 'Командные комнаты'
const BASE_URL = 'http://127.0.0.1:8777/'
const RETRY_MS = 5000
const WATCHDOG_MS = 12000

// ── SDK-двери: всё необязательное ───────────────────────────────────────────
const React = ReactNS && ReactNS.useEffect ? ReactNS : ReactNS && ReactNS.default ? ReactNS.default : {}
const jsx = (JSXNS && (JSXNS.jsx || (JSXNS.default && JSXNS.default.jsx))) || null
const jsxs = (JSXNS && (JSXNS.jsxs || (JSXNS.default && JSXNS.default.jsxs))) || jsx
const host = (SDK && SDK.host) || {}

const ROUTES_AREA = (SDK && SDK.ROUTES_AREA) || 'routes'
const SIDEBAR_NAV_AREA = (SDK && SDK.SIDEBAR_NAV_AREA) || 'sidebar.nav'
const PALETTE_AREA = (SDK && SDK.PALETTE_AREA) || 'palette'

const warn = message => {
  try {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[team-room] ' + message)
    }
  } catch {
    // молчим: журнал не должен ломать плагин
  }
}

// ── Тема: только через документ, с защитой ──────────────────────────────────
function detectTheme() {
  try {
    const root = typeof document !== 'undefined' ? document.documentElement : null

    if (!root) {
      return 'light'
    }

    const attr = String(
      root.getAttribute('data-theme') ||
        root.getAttribute('data-color-scheme') ||
        root.getAttribute('data-appearance') ||
        ''
    ).toLowerCase()

    if (attr.indexOf('dark') >= 0) {
      return 'dark'
    }

    if (attr.indexOf('light') >= 0) {
      return 'light'
    }

    if (root.classList) {
      if (root.classList.contains('dark')) {
        return 'dark'
      }

      if (root.classList.contains('light')) {
        return 'light'
      }
    }

    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      const media = window.matchMedia('(prefers-color-scheme: dark)')

      if (media && media.matches) {
        return 'dark'
      }
    }
  } catch {
    // не смогли прочитать тему — светлая по умолчанию
  }

  return 'light'
}

function frameUrl() {
  // B-16: при пересоздании webview открываем ТОТ ЖЕ маршрут, а не «дом».
  return BASE_URL + '?theme=' + detectTheme() + (lastHash ? lastHash : '')
}

// ── Память маршрута (B-16) ──────────────────────────────────────────────────
/* Страница сообщает маршрут: `postMessage({type:'team-room:route', hash})`.
   В браузере и в <iframe> это доходит до хозяина напрямую; в <webview> между
   процессами postMessage не ходит, поэтому там же читаем адрес гостя
   (`did-navigate-in-page` + `location.hash`) — источник правды один: адрес. */
let lastHash = ''

/** Хэш из строки вида `#/room/…` или полного адреса. '' — не наш маршрут. */
function hashOf(value) {
  const raw = String(value == null ? '' : value).trim()
  if (!raw) return ''
  if (raw.charAt(0) === '#') return raw.length <= 512 ? raw : ''
  try {
    const url = new URL(raw)
    return String(url.hash || '').slice(0, 512)
  } catch {
    return ''
  }
}

/**
 * Запомнить маршрут гостя. `#` и `#/` — это «дом», старый маршрут забываем;
 * пустое/непонятное значение память не портит.
 * @returns {string} актуальный хэш
 */
function rememberRoute(value) {
  const hash = hashOf(value)
  if (!hash) return lastHash
  if (hash === '#' || hash === '#/') {
    lastHash = ''
    return lastHash
  }
  if (hash.charAt(0) === '#' && hash.charAt(1) === '/') lastHash = hash
  return lastHash
}

/** Прочитать адрес гостя (webview: без этого postMessage-путь недоступен). */
function readGuestHash(view) {
  if (!view || typeof view.executeJavaScript !== 'function') return
  try {
    const result = view.executeJavaScript('location.hash')
    if (result && typeof result.then === 'function') {
      result.then(hash => rememberRoute(hash)).catch(() => {
        // страница ещё грузится или уже уехала
      })
    }
  } catch {
    // webview не готов — маршрут придёт следующим событием
  }
}

/** Сообщение страницы о маршруте (iframe и встроенные варианты). */
function routeFromMessage(data) {
  if (!data || typeof data !== 'object') return ''
  if (data.type !== 'team-room:route') return ''
  return rememberRoute(data.hash)
}

// ── Тема хозяина: читаем токены и передаём странице ─────────────────────────
/* Страница комнаты умеет принимать тему (web/js/theme-bridge.js): поверхности,
   текст, штрихи, акцент и радиус. Здесь мы ТОЛЬКО читаем вычисленные значения
   у приложения-хозяина — ничего не пишем в его DOM (правило волны 17). */
const HOST_TOKENS = [
  '--ui-bg-chrome', '--ui-bg-sidebar', '--ui-bg-editor', '--ui-bg-elevated',
  '--ui-accent', '--ui-base',
  '--ui-text-primary', '--ui-text-secondary', '--ui-text-tertiary', '--ui-text-quaternary',
  '--ui-stroke-primary', '--ui-stroke-secondary', '--ui-stroke-tertiary', '--ui-stroke-quaternary'
]
const RADIUS_TOKENS = ['--ui-radius-lg', '--ui-radius', '--radius-lg', '--radius']

function hostThemePayload() {
  const payload = { type: 'team-room:theme', version: 1, scheme: detectTheme(), tokens: {} }

  try {
    const root = document.documentElement
    const nodes = [root, document.body]

    try {
      const extra = document.querySelectorAll('[data-theme], .app, #root')
      for (let i = 0; i < extra.length && nodes.length < 6; i += 1) {
        nodes.push(extra[i])
      }
    } catch {
      // селектор не сработал — читаем с :root и body
    }

    const tokens = {}
    let radius = null

    for (const node of nodes) {
      if (!node) continue
      const style = window.getComputedStyle(node)
      if (!style) continue

      for (const name of HOST_TOKENS) {
        if (tokens[name]) continue
        const value = String(style.getPropertyValue(name) || '').trim()
        if (value) tokens[name] = value
      }

      if (radius === null) {
        for (const name of RADIUS_TOKENS) {
          const value = parseFloat(String(style.getPropertyValue(name) || '').trim())
          if (Number.isFinite(value) && value > 0 && value <= 32) {
            radius = Math.round(value)
            break
          }
        }
      }

      if (Object.keys(tokens).length >= HOST_TOKENS.length && radius !== null) break
    }

    payload.tokens = tokens
    if (radius !== null) payload.radius = radius
  } catch (error) {
    warn('чтение токенов темы: ' + String((error && error.message) || error))
  }

  return payload
}

/** Отдать тему странице внутри <webview> (executeJavaScript → window.postMessage). */
function sendThemeToView(view) {
  if (!view || typeof view.executeJavaScript !== 'function') return

  try {
    const code = 'window.postMessage(' + JSON.stringify(hostThemePayload()) + ", '*');"
    const result = view.executeJavaScript(code)

    if (result && typeof result.catch === 'function') {
      result.catch(() => {
        // страница ещё грузится — отдадим по did-finish-load
      })
    }
  } catch (error) {
    warn('передача темы в webview: ' + String((error && error.message) || error))
  }
}

/** Отдать тему странице в <iframe> (у iframe есть contentWindow). */
function sendThemeToFrame(frame) {
  try {
    const target = frame && frame.contentWindow
    if (!target) return
    target.postMessage(hostThemePayload(), '*')
  } catch (error) {
    warn('передача темы в iframe: ' + String((error && error.message) || error))
  }
}

function openInBrowser(url) {
  try {
    const opener = host && host.os && host.os.openExternal

    if (typeof opener === 'function') {
      opener(url)
      return true
    }
  } catch (error) {
    warn('openExternal: ' + String((error && error.message) || error))
  }

  try {
    if (typeof window !== 'undefined' && typeof window.open === 'function') {
      window.open(url, '_blank', 'noopener')
      return true
    }
  } catch {
    // ниже — честный отказ
  }

  return false
}

// ── Оформление: только токены темы ──────────────────────────────────────────
const textPrimary = { color: 'var(--ui-text-secondary)' }
const textMuted = { color: 'var(--ui-text-tertiary)' }

const buttonStyle = {
  appearance: 'none',
  border: '1px solid var(--ui-stroke-secondary, var(--ui-stroke-quaternary))',
  background: 'transparent',
  color: 'var(--ui-text-secondary)',
  borderRadius: '0.5rem',
  cursor: 'pointer',
  font: 'inherit',
  fontSize: '0.8125rem',
  padding: '0.375rem 0.75rem'
}

const primaryButtonStyle = {
  ...buttonStyle,
  background: 'var(--ui-accent)',
  borderColor: 'var(--ui-accent)',
  color: 'var(--ui-bg-chrome, var(--ui-text-secondary))',
  fontWeight: 600
}

const overlayStyle = {
  alignItems: 'center',
  background: 'var(--ui-bg-chrome, transparent)',
  bottom: 0,
  display: 'flex',
  justifyContent: 'center',
  left: 0,
  padding: '1.5rem',
  position: 'absolute',
  right: 0,
  top: 0
}

const cardStyle = {
  display: 'flex',
  flexDirection: 'column',
  gap: '0.75rem',
  maxWidth: '30rem',
  textAlign: 'center'
}

// Плашка «сервер перезапускается»: страница остаётся живой, ничего не сносим.
const offlineStyle = {
  alignItems: 'center',
  background: 'var(--ui-bg-elevated, rgba(127,127,127,0.12))',
  borderBottom: '1px solid var(--ui-stroke-tertiary, rgba(127,127,127,0.25))',
  color: 'var(--ui-text-tertiary)',
  display: 'flex',
  fontSize: '0.75rem',
  justifyContent: 'center',
  left: 0,
  padding: '0.25rem 0.75rem',
  pointerEvents: 'none',
  position: 'absolute',
  right: 0,
  top: 0
}

// ── Страница ────────────────────────────────────────────────────────────────
function TeamRoomPage() {
  const { useState, useRef, useEffect, useCallback } = React
  const [mode, setMode] = useState('webview') // 'webview' | 'iframe'
  // 'offline' — страница уже была загружена и жива, но сервер сейчас недоступен.
  const [status, setStatus] = useState('loading') // 'loading' | 'ready' | 'offline' | 'failed'
  const [detail, setDetail] = useState('')
  const [tick, setTick] = useState(0)
  const holder = useRef(null)
  const guest = useRef(null)

  const retry = useCallback(() => {
    setDetail('')
    setStatus('loading')
    setTick(value => value + 1)
  }, [])

  useEffect(() => {
    const box = holder.current

    if (!box || !jsx) {
      return undefined
    }

    let disposed = false
    let watchdog = null
    let pushGuest = null
    // Страница хоть раз загрузилась в этом webview: её больше не сносим.
    let loaded = false
    const src = frameUrl()

    const clear = () => {
      try {
        box.replaceChildren()
      } catch {
        while (box.firstChild) {
          box.removeChild(box.firstChild)
        }
      }
    }

    const onReady = () => {
      if (disposed) {
        return
      }

      loaded = true
      setStatus('ready')
      setDetail('')

      // Тема хозяина — после загрузки страницы: её модули к этому моменту поднялись.
      setTimeout(() => {
        if (disposed || !pushGuest) {
          return
        }

        try {
          pushGuest()
        } catch {
          // страница уже могла уехать — не беда
        }
      }, 250)
    }

    const onFailed = (errorCode, errorDescription) => {
      if (disposed) {
        return
      }

      // -3 = ERR_ABORTED (обычное дело при быстром повторном переходе)
      if (errorCode === -3) {
        return
      }

      // B-16: кратковременная недоступность сервера (перезапуск ~10 с) НЕ должна
      // разрушать уже загруженную страницу: не пересоздаём её и не сбрасываем
      // маршрут — страница сама переподключится к API. Только тонкая плашка.
      if (loaded) {
        setStatus('offline')
        setDetail(String(errorDescription || errorCode || ''))
        return
      }

      setStatus('failed')
      setDetail(String(errorDescription || errorCode || ''))
    }

    clear()

    const armWatchdog = () => {
      watchdog = setTimeout(() => {
        if (!disposed) {
          setStatus(current => (current === 'loading' ? 'failed' : current))
        }
      }, WATCHDOG_MS)
    }

    if (mode === 'webview') {
      let view = null

      try {
        view = document.createElement('webview')
      } catch (error) {
        warn('webview недоступен: ' + String((error && error.message) || error))
        view = null
      }

      if (!view) {
        setMode('iframe')
        return undefined
      }

      // НИКАКОГО partition: гостю нужен defaultSession — на нём приложение уже
      // выдало права на микрофон (electron/media-capture-permission.ts).
      view.setAttribute('src', src)
      view.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no')

      try {
        view.setAttribute('allow', 'microphone; clipboard-write')
      } catch {
        // старые сборки не знают этот атрибут — не беда
      }

      view.style.cssText = 'flex:1 1 auto;width:100%;height:100%;border:0;display:flex'

      const handleFail = event => {
        const data = event || {}
        // did-fail-load приходит и по подкадрам — смотрим только главный
        if (data.isMainFrame === false) {
          return
        }

        onFailed(data.errorCode, data.errorDescription)
      }

      const handleCrash = () => {
        // Окно гостя умерло — пересоздадим (маршрут уже запомнен событиями
        // навигации, поэтому откроется то же место).
        if (disposed) {
          return
        }
        setStatus('failed')
        setDetail('окно модуля закрылось')
      }

      // B-16: адрес гостя — источник правды о маршруте. Ставим его же в
      // lastHash, чтобы пересоздание открыло то же место.
      const handleNavigate = event => {
        const data = event || {}
        if (data.isMainFrame === false) {
          return
        }
        rememberRoute(data.url || (typeof view.getURL === 'function' ? view.getURL() : ''))
        readGuestHash(view)
        setStatus(current => (current === 'offline' ? 'ready' : current))
      }

      try {
        view.addEventListener('did-fail-load', handleFail)
        view.addEventListener('did-finish-load', onReady)
        view.addEventListener('crashed', handleCrash)
        view.addEventListener('render-process-gone', handleCrash)
        view.addEventListener('did-navigate', handleNavigate)
        view.addEventListener('did-navigate-in-page', handleNavigate)
        view.addEventListener('dom-ready', () => readGuestHash(view))
      } catch (error) {
        warn('события webview: ' + String((error && error.message) || error))
      }

      box.appendChild(view)
      guest.current = { kind: 'webview', node: view }
      pushGuest = () => sendThemeToView(view)

      // Тег <webview> существует, только если приложение его разрешило.
      // Проверяем сам факт: у настоящего webview есть getWebContentsId.
      const usable = typeof view.getWebContentsId === 'function'
      armWatchdog()

      if (!usable) {
        warn('тег <webview> не активен — перехожу на <iframe>')
        setMode('iframe')
        return undefined
      }

      return () => {
        disposed = true
        clearTimeout(watchdog)
        try {
          view.removeEventListener('did-fail-load', handleFail)
          view.removeEventListener('did-finish-load', onReady)
          view.removeEventListener('crashed', handleCrash)
          view.removeEventListener('render-process-gone', handleCrash)
          view.removeEventListener('did-navigate', handleNavigate)
          view.removeEventListener('did-navigate-in-page', handleNavigate)
        } catch {
          // элемент уже мог уехать
        }

        try {
          view.remove()
        } catch {
          clear()
        }
      }
    }

    // Запасной путь: <iframe>. Сервер отдаёт X-Frame-Options: SAMEORIGIN,
    // поэтому этот путь может быть заблокирован самим сервером — тогда
    // остаётся кнопка «Открыть в браузере».
    let frame = null

    try {
      frame = document.createElement('iframe')
    } catch {
      frame = null
    }

    if (!frame) {
      setStatus('failed')
      return undefined
    }

    frame.setAttribute('allow', 'microphone; clipboard-write; autoplay')
    frame.setAttribute('src', src)
    frame.style.cssText = 'flex:1 1 auto;width:100%;height:100%;border:0;display:block'

    frame.addEventListener('load', onReady)
    frame.addEventListener('error', () => onFailed(0, ''))
    box.appendChild(frame)
    guest.current = { kind: 'iframe', node: frame }
    pushGuest = () => sendThemeToFrame(frame)
    armWatchdog()

    return () => {
      disposed = true
      clearTimeout(watchdog)
      try {
        frame.removeEventListener('load', onReady)
      } catch {
        // ничего
      }

      clear()
    }
  }, [mode, tick])

  // Тема хозяина: отдаём странице при старте и при каждой смене темы приложения.
  // Следим за атрибутами корня/тела и системной схемой — Hermes меняет их сам,
  // когда пользователь переключает тему или правит оформление в настройках.
  useEffect(() => {
    const push = () => {
      const current = guest.current
      if (!current || !current.node) {
        return
      }

      if (current.kind === 'webview') {
        sendThemeToView(current.node)
      } else {
        sendThemeToFrame(current.node)
      }
    }

    // Странице нужно время сменить режим загрузки — первый заход дублируем.
    const first = setTimeout(push, 1200)
    let pending = null
    const schedule = () => {
      if (pending) {
        clearTimeout(pending)
      }

      pending = setTimeout(push, 150)
    }

    let observer = null

    try {
      if (typeof MutationObserver === 'function') {
        observer = new MutationObserver(schedule)
        const filter = ['data-theme', 'data-color-scheme', 'data-appearance', 'class', 'style']
        observer.observe(document.documentElement, { attributes: true, attributeFilter: filter })

        if (document.body) {
          observer.observe(document.body, { attributes: true, attributeFilter: filter })
        }
      }
    } catch (error) {
      warn('наблюдение за темой: ' + String((error && error.message) || error))
    }

    let media = null

    try {
      media = window.matchMedia('(prefers-color-scheme: dark)')
      if (media && typeof media.addEventListener === 'function') {
        media.addEventListener('change', schedule)
      }
    } catch {
      // нет matchMedia — тема придёт по следующей загрузке страницы
    }

    // Безопасный запрос страницы: «дай тему» — отвечаем только своим гостем.
    // Заодно принимаем сообщение о маршруте (B-16): так его присылает страница
    // в <iframe>; в <webview> адрес читается событиями навигации.
    const onRequest = (event) => {
      const data = event && event.data
      if (data && data.type === 'team-room:theme:request') {
        push()
      }
      if (routeFromMessage(data) && guest.current) {
        setStatus(current => (current === 'offline' ? 'ready' : current))
      }
    }

    try {
      window.addEventListener('message', onRequest)
    } catch {
      // окно хозяина закрыто
    }

    return () => {
      clearTimeout(first)
      if (pending) {
        clearTimeout(pending)
      }

      try {
        if (observer) {
          observer.disconnect()
        }
      } catch {
        // уже отключён
      }

      try {
        if (media && typeof media.removeEventListener === 'function') {
          media.removeEventListener('change', schedule)
        }
      } catch {
        // ничего
      }

      try {
        window.removeEventListener('message', onRequest)
      } catch {
        // ничего
      }
    }
  }, [mode, tick])

  // Автоповтор, пока сервер ещё не поднялся: раз в 5 секунд.
  useEffect(() => {
    if (status !== 'failed') {
      return undefined
    }

    const timer = setTimeout(retry, RETRY_MS)

    return () => clearTimeout(timer)
  }, [status, tick, retry])

  const failed = status === 'failed'

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      minHeight: 0,
      position: 'relative',
      width: '100%'
    },
    children: [
      jsx('div', {
        ref: holder,
        style: { display: 'flex', flex: '1 1 auto', minHeight: 0, width: '100%' }
      }),
      status === 'offline'
        ? jsx('div', {
            style: offlineStyle,
            role: 'status',
            children: 'Сервер перезапускается: страница останется открытой, данные подтянутся сами.'
          })
        : null,
      failed
        ? jsx('div', {
            style: overlayStyle,
            children: jsxs('div', {
              style: cardStyle,
              children: [
                jsx('div', {
                  style: { ...textPrimary, fontSize: '1rem', fontWeight: 600 },
                  children: 'Командные комнаты ещё запускаются'
                }),
                jsx('div', {
                  style: { ...textMuted, fontSize: '0.875rem', lineHeight: 1.5 },
                  children: 'Обычно это занимает до двух минут после входа в Windows. Как только всё будет готово, страница откроется сама.'
                }),
                detail
                  ? jsx('div', {
                      style: { ...textMuted, fontSize: '0.75rem' },
                      children: 'Подробность: ' + detail
                    })
                  : null,
                jsxs('div', {
                  style: { display: 'flex', gap: '0.5rem', justifyContent: 'center', marginTop: '0.25rem' },
                  children: [
                    jsx('button', {
                      type: 'button',
                      style: primaryButtonStyle,
                      onClick: retry,
                      children: 'Повторить'
                    }),
                    jsx('button', {
                      type: 'button',
                      style: buttonStyle,
                      onClick: () => openInBrowser(frameUrl()),
                      children: 'Открыть в браузере'
                    })
                  ]
                })
              ]
            })
          })
        : null
    ]
  })
}

// ── Регистрация ─────────────────────────────────────────────────────────────
function register(ctx) {
  const register = (contribution, label) => {
    try {
      const disposer = ctx && typeof ctx.register === 'function' ? ctx.register(contribution) : null

      if (!disposer) {
        warn('не удалось зарегистрировать: ' + label)
      }

      return disposer
    } catch (error) {
      warn('ошибка регистрации ' + label + ': ' + String((error && error.message) || error))
      return null
    }
  }

  // Страница на всю область
  register(
    {
      id: 'page',
      area: ROUTES_AREA,
      title: NAME,
      data: { path: '/team-room' },
      render: () => (jsx ? jsx(TeamRoomPage, {}) : null)
    },
    'страница /team-room'
  )

  // Строка в левом меню — сразу под «Канбаном» (у него order 50)
  register(
    {
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 55,
      data: { codicon: 'organization', label: NAME, path: '/team-room' }
    },
    'строка меню'
  )

  // Команда в палитре: Ctrl+K → «Командные комнаты»
  register(
    {
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'team-room.open',
        label: NAME,
        keywords: ['командные', 'комнаты', 'команда', 'team', 'room', 'rooms', '8777'],
        run: () => {
          try {
            if (host && typeof host.navigate === 'function') {
              host.navigate('/team-room')
            }
          } catch (error) {
            warn('navigate: ' + String((error && error.message) || error))
          }
        }
      }
    },
    'команда палитры'
  )
}

export default {
  id: ID,
  name: NAME,
  description:
    'Комнатная страница сервера «Комната команды» (127.0.0.1:8777) прямо в приложении: строка в левом меню, страница на всю область и команда в палитре.',
  register(ctx) {
    try {
      register(ctx)
      warn('плагин v' + PLUGIN_VERSION + ' загружен')
    } catch (error) {
      warn('register() упал целиком: ' + String((error && error.message) || error))
    }
  }
}
