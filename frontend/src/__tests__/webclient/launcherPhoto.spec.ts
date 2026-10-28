/*
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLauncherPhoto, resolveLauncherPhotoUrl } from '@/webclient/launcher-photo'

/**
 * The launcher face. When the agent has a photo the floating button shows it
 * instead of the pulsing mark and the chevron; without one nothing changes.
 */
describe('resolveLauncherPhotoUrl', () => {
  const API_BASE = 'https://api.example.com/api/v1'

  it('resolves a relative upload path against the widget API origin', () => {
    expect(resolveLauncherPhotoUrl('/api/v1/uploads/agents/org/a.png', API_BASE)).toBe(
      'https://api.example.com/api/v1/uploads/agents/org/a.png'
    )
  })

  it('passes an absolute signed URL through untouched', () => {
    const signed = 'https://bucket.s3.amazonaws.com/agents/a.png?X-Amz-Signature=abc'
    expect(resolveLauncherPhotoUrl(signed, API_BASE)).toBe(signed)
  })

  it('returns an empty string when there is no photo', () => {
    expect(resolveLauncherPhotoUrl(null, API_BASE)).toBe('')
    expect(resolveLauncherPhotoUrl(undefined, API_BASE)).toBe('')
    expect(resolveLauncherPhotoUrl('', API_BASE)).toBe('')
  })

  it('tolerates an API base that is not suffixed with /api/v1', () => {
    expect(resolveLauncherPhotoUrl('/api/v1/uploads/a.png', 'https://h.example')).toBe(
      'https://h.example/api/v1/uploads/a.png'
    )
  })
})

describe('createLauncherPhoto', () => {
  it('builds an image with the launcher class and the resolved src', () => {
    const img = createLauncherPhoto('https://h.example/api/v1/uploads/a.png', document)
    expect(img).not.toBeNull()
    expect(img!.tagName).toBe('IMG')
    expect(img!.className).toBe('cm-photo')
    expect(img!.getAttribute('src')).toBe('https://h.example/api/v1/uploads/a.png')
    expect(img!.getAttribute('alt')).toBe('')
  })

  it('builds nothing when there is no source', () => {
    expect(createLauncherPhoto('', document)).toBeNull()
  })

  it('treats the photo URL as data, not markup', () => {
    // An org could store a quote in photo_url; a string-built <img> would break the
    // element out and inject a handler. The DOM API keeps it inside src.
    const hostile = 'https://h.example/"><img src=x onerror=alert(1)>'
    const img = createLauncherPhoto(hostile, document)
    expect(img!.getAttribute('src')).toBe(hostile)
    expect(img!.children).toHaveLength(0)
  })
})

describe('launcher face in the embed loader', () => {
  const WIDGET_ID = 'w-123'
  const BASE_URL = 'https://api.example.com/api/v1'
  const html = '<!DOCTYPE html><html><body>widget</body></html>'

  let fetchMock: ReturnType<typeof vi.fn>
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

  async function loadLoader() {
    vi.resetModules()
    await import('@/webclient/chattermate.js')
    await flush()
  }

  function button(): HTMLElement {
    return document.getElementById('chattermate-button') as HTMLElement
  }

  /** A message as the widget frame would post it. */
  function fromFrame(data: Record<string, unknown>) {
    const frame = document.querySelector('.chattermate-iframe') as HTMLIFrameElement
    window.dispatchEvent(new MessageEvent('message', { data, source: frame.contentWindow }))
  }

  beforeEach(() => {
    document.body.innerHTML = ''
    document.head.innerHTML = ''
    localStorage.clear()
    // @ts-expect-error loader globals set by the embed snippet
    window.chattermateId = WIDGET_ID
    // @ts-expect-error loader globals set by the embed snippet
    window.chattermateBaseUrl = BASE_URL
    fetchMock = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(html) })
    )
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // @ts-expect-error cleanup loader globals
    delete window.ChatterMate
  })

  it('keeps the mark and the chevron when there is no photo', async () => {
    await loadLoader()
    fromFrame({ type: 'CUSTOMIZATION_UPDATE', data: { chat_bubble_color: '#123456' } })

    const btn = button()
    expect(btn.querySelector('.cm-photo')).toBeNull()
    expect(btn.classList.contains('has-photo')).toBe(false)
    expect(btn.querySelector('.cm-dots')).not.toBeNull()
    expect(btn.querySelector('.cm-chevron')).not.toBeNull()
  })

  it('swaps in the agent photo when the customization carries one', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/agents/org/a.png' },
    })

    const btn = button()
    const img = btn.querySelector('.cm-photo') as HTMLImageElement
    expect(img).not.toBeNull()
    expect(img.tagName).toBe('IMG')
    expect(img.getAttribute('src')).toBe(
      'https://api.example.com/api/v1/uploads/agents/org/a.png'
    )
    expect(btn.classList.contains('has-photo')).toBe(true)
    // The mark stays in the DOM, hidden by the has-photo rule, so clearing the
    // photo needs no re-render of the button's children.
    expect(btn.querySelector('.cm-dots')).not.toBeNull()
  })

  it('does not rebuild the image when the same photo arrives again', async () => {
    await loadLoader()
    const data = { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/agents/a.png' }
    fromFrame({ type: 'CUSTOMIZATION_UPDATE', data })
    const first = button().querySelector('.cm-photo')
    fromFrame({ type: 'CUSTOMIZATION_UPDATE', data })

    expect(button().querySelector('.cm-photo')).toBe(first)
  })

  it('goes back to the mark when the photo is removed', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/agents/a.png' },
    })
    fromFrame({ type: 'CUSTOMIZATION_UPDATE', data: { chat_bubble_color: '#123456' } })

    expect(button().querySelector('.cm-photo')).toBeNull()
    expect(button().classList.contains('has-photo')).toBe(false)
  })

  it('ignores a non-string photo_url', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: { href: '/a.png' } },
    })

    expect(button().querySelector('.cm-photo')).toBeNull()
  })

  it('falls back to the mark when the image fails to load', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/gone.png' },
    })
    const img = button().querySelector('.cm-photo') as HTMLImageElement
    img.dispatchEvent(new Event('error'))

    expect(button().querySelector('.cm-photo')).toBeNull()
    expect(button().classList.contains('has-photo')).toBe(false)
    // The transparent state is keyed on the class alone, so dropping it restores the
    // brand bubble, its shadow and the mark.
    expect(
      (document.getElementById('chattermate-styles')?.textContent ?? '').replace(/\s+/g, ' ')
    ).toContain('#chattermate-button { position: fixed;')
  })

  it('ignores a stale photo failing after a newer one replaced it', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/old.png' },
    })
    const old = button().querySelector('.cm-photo') as HTMLImageElement

    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/new.png' },
    })
    // The old request finally errors — the new photo must stay on the button.
    old.dispatchEvent(new Event('error'))

    const current = button().querySelector('.cm-photo') as HTMLImageElement
    expect(current).not.toBeNull()
    expect(current.getAttribute('src')).toBe('https://api.example.com/api/v1/uploads/new.png')
    expect(button().classList.contains('has-photo')).toBe(true)
  })

  it('leaves the open/closed toggle and the unread badge intact', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/agents/a.png' },
    })

    const btn = button()
    expect(btn.querySelector('.cm-badge')).not.toBeNull()

    btn.click()
    expect(btn.classList.contains('active')).toBe(true)
    // The photo is the face in both states — it does not toggle with `active`.
    expect(btn.querySelector('.cm-photo')).not.toBeNull()

    btn.click()
    expect(btn.classList.contains('active')).toBe(false)
  })

  /** The injected launcher stylesheet, whitespace-normalised for matching. */
  function stylesheet(): string {
    return (document.getElementById('chattermate-styles')?.textContent ?? '').replace(/\s+/g, ' ')
  }

  /** The declaration body of one rule, addressed by its exact selector. */
  function ruleBody(selector: string): string {
    const css = stylesheet()
    const start = css.indexOf(`${selector} {`)
    if (start === -1) return ''
    return css.slice(start, css.indexOf('}', start))
  }

  it('makes the launcher transparent behind the agent photo', async () => {
    await loadLoader()
    fromFrame({
      type: 'CUSTOMIZATION_UPDATE',
      data: { chat_bubble_color: '#123456', photo_url: '/api/v1/uploads/agents/a.png' },
    })

    // Only the image is left: no brand bubble, no drop shadow, no inset ring.
    const body = ruleBody('#chattermate-button.has-photo')
    expect(body).toContain('background: transparent')
    expect(body).toContain('box-shadow: none')
    // The spinner was drawn for the bubble; transparent, it needs the brand colour.
    expect(ruleBody('#chattermate-button.has-photo.loading:after')).toContain('border-top-color')
  })

  it('fits the photo inside the button box without cropping or clipping it', async () => {
    await loadLoader()

    const body = ruleBody('#chattermate-button .cm-photo')
    // contain, not cover: a cut-out mascot must not be cropped by the square box.
    expect(body).toContain('object-fit: contain')
    // The image follows the button's own size (64px, 56px on the mobile rule).
    expect(body).toContain('inset: 0')
    expect(body).toContain('width: 100%')
    expect(body).toContain('height: 100%')
    expect(body).not.toContain('border-radius')
  })

  it('keeps the brand bubble and its shadow when no photo is present', async () => {
    await loadLoader()
    fromFrame({ type: 'CUSTOMIZATION_UPDATE', data: { chat_bubble_color: '#123456' } })

    expect(button().classList.contains('has-photo')).toBe(false)
    const body = ruleBody('#chattermate-button')
    expect(body).toContain('background:')
    expect(body).toContain('box-shadow: 0 16px 40px')
    expect(body).toContain('inset 0 0 0 1px')
  })
})
