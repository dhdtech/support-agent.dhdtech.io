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

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
    playAlertChime,
    requestWidgetNotificationPermission,
    shouldAlertAgentMessage,
    showAgentNotification,
    useWidgetAttention,
} from '@/composables/useWidgetAttention'

/**
 * A visitor must hear about a human reply they are not looking at — and must not be
 * interrupted by one that is already on screen.
 */
describe('shouldAlertAgentMessage', () => {
    it('does not alert for a bot reply, whatever the state', () => {
        expect(shouldAlertAgentMessage('bot', false, true)).toBe(false)
        expect(shouldAlertAgentMessage(undefined, false, false)).toBe(false)
    })

    it('does not alert when the widget is open and the tab is visible', () => {
        expect(shouldAlertAgentMessage('agent_message', true, false)).toBe(false)
    })

    it('alerts when the widget is closed', () => {
        expect(shouldAlertAgentMessage('agent_message', false, false)).toBe(true)
    })

    it('alerts when the tab is hidden, even with the widget open', () => {
        expect(shouldAlertAgentMessage('agent_message', true, true)).toBe(true)
    })
})

type AttentionHarness = ReturnType<typeof makeAttention>

const makeAttention = (overrides: Record<string, unknown> = {}) => {
    const spies = {
        onUnread: vi.fn(),
        onClear: vi.fn(),
        playSound: vi.fn(),
        notify: vi.fn(),
        primeAudio: vi.fn(),
        requestPermission: vi.fn(),
    }
    let open = false
    let hidden = false
    const attention = useWidgetAttention({
        isWidgetOpen: () => open,
        isDocumentHidden: () => hidden,
        ...spies,
        ...overrides,
    })
    return {
        attention,
        spies,
        setOpen: (value: boolean) => {
            open = value
        },
        setHidden: (value: boolean) => {
            hidden = value
        },
    }
}

describe('useWidgetAttention', () => {
    let harness: AttentionHarness | null = null

    afterEach(() => {
        harness?.attention.stop()
        harness = null
        vi.restoreAllMocks()
        vi.unstubAllGlobals()
    })

    it('chimes, badges and notifies on a hidden tab', () => {
        harness = makeAttention()
        harness.setOpen(true)
        harness.setHidden(true)

        harness.attention.handleAgentMessage({
            type: 'agent_message',
            agent_name: 'Ada',
            message: 'I am taking over this chat',
        })

        expect(harness.spies.onUnread).toHaveBeenCalledTimes(1)
        expect(harness.spies.playSound).toHaveBeenCalledTimes(1)
        expect(harness.spies.notify).toHaveBeenCalledWith('Ada', 'I am taking over this chat')
    })

    it('badges and chimes but does not notify when the widget is closed and visible', () => {
        harness = makeAttention()
        harness.setOpen(false)
        harness.setHidden(false)

        harness.attention.handleAgentMessage({ type: 'agent_message', message: 'hello' })

        expect(harness.spies.onUnread).toHaveBeenCalledTimes(1)
        expect(harness.spies.playSound).toHaveBeenCalledTimes(1)
        expect(harness.spies.notify).not.toHaveBeenCalled()
    })

    it('stays silent when the visitor is already looking at the reply', () => {
        harness = makeAttention()
        harness.setOpen(true)
        harness.setHidden(false)

        harness.attention.handleAgentMessage({ type: 'agent_message', message: 'hello' })

        expect(harness.spies.onUnread).not.toHaveBeenCalled()
        expect(harness.spies.playSound).not.toHaveBeenCalled()
        expect(harness.spies.notify).not.toHaveBeenCalled()
    })

    it('ignores bot replies even when the widget is closed', () => {
        harness = makeAttention()

        harness.attention.handleAgentMessage({ type: 'bot', message: 'hi there' })

        expect(harness.spies.onUnread).not.toHaveBeenCalled()
        expect(harness.spies.playSound).not.toHaveBeenCalled()
    })

    it('never lets a broken sound or notification throw into the socket', () => {
        vi.spyOn(console, 'error').mockImplementation(() => {})
        harness = makeAttention({
            playSound: vi.fn(() => {
                throw new Error('audio blocked')
            }),
            notify: vi.fn(() => {
                throw new Error('notification blocked')
            }),
        })
        harness.setOpen(true)
        harness.setHidden(true)

        expect(() =>
            harness!.attention.handleAgentMessage({ type: 'agent_message', message: 'hello' })
        ).not.toThrow()
        expect(harness.spies.onUnread).toHaveBeenCalledTimes(1)
    })

    it('primes audio and asks for permission only on the first interaction', () => {
        harness = makeAttention()

        harness.attention.prime()
        harness.attention.prime()

        expect(harness.spies.primeAudio).toHaveBeenCalledTimes(1)
        expect(harness.spies.requestPermission).toHaveBeenCalledTimes(1)
    })

    it('clears the badge when the visitor returns to an open widget', () => {
        harness = makeAttention()
        harness.setOpen(true)
        harness.setHidden(false)

        document.dispatchEvent(new Event('visibilitychange'))

        expect(harness.spies.onClear).toHaveBeenCalledTimes(1)
    })

    it('leaves the badge alone when the tab becomes visible with the widget closed', () => {
        harness = makeAttention()
        harness.setOpen(false)
        harness.setHidden(false)

        document.dispatchEvent(new Event('visibilitychange'))

        expect(harness.spies.onClear).not.toHaveBeenCalled()
    })
})

describe('agent notification', () => {
    class FakeNotification {
        static permission: NotificationPermission = 'granted'
        static requestPermission = vi.fn().mockResolvedValue('granted')
        static instances: FakeNotification[] = []

        onclick: (() => void) | null = null
        close = vi.fn()

        constructor(
            public title: string,
            public options: NotificationOptions
        ) {
            FakeNotification.instances.push(this)
        }
    }

    afterEach(() => {
        FakeNotification.instances = []
        FakeNotification.requestPermission.mockClear()
        vi.restoreAllMocks()
        vi.unstubAllGlobals()
    })

    it('shows the agent name and a trimmed message', () => {
        vi.stubGlobal('Notification', FakeNotification)

        showAgentNotification('Ada', 'x'.repeat(300))

        expect(FakeNotification.instances).toHaveLength(1)
        expect(FakeNotification.instances[0].title).toBe('Ada')
        const body = FakeNotification.instances[0].options.body as string
        expect(body.length).toBeLessThanOrEqual(120)
        expect(body.endsWith('…')).toBe(true)
    })

    it('does nothing without permission', () => {
        FakeNotification.permission = 'default'
        vi.stubGlobal('Notification', FakeNotification)

        showAgentNotification('Ada', 'hello')

        expect(FakeNotification.instances).toHaveLength(0)
        FakeNotification.permission = 'granted'
    })

    it('opens the widget when the notification is clicked', () => {
        vi.stubGlobal('Notification', FakeNotification)
        vi.spyOn(window, 'focus').mockImplementation(() => {})
        const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})

        showAgentNotification('Ada', 'hello')
        FakeNotification.instances[0].onclick?.()

        expect(post).toHaveBeenCalledWith({ type: 'WIDGET_OPEN' }, expect.anything())
        expect(FakeNotification.instances[0].close).toHaveBeenCalled()
    })

    it('does not ask again once the visitor has decided', () => {
        FakeNotification.permission = 'default'
        vi.stubGlobal('Notification', FakeNotification)

        requestWidgetNotificationPermission()
        requestWidgetNotificationPermission()

        expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(2)

        FakeNotification.permission = 'denied'
        requestWidgetNotificationPermission()
        expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(2)
    })
})

describe('alert chime', () => {
    it('stays quiet instead of throwing when the browser has no audio context', () => {
        expect(() => playAlertChime()).not.toThrow()
    })
})
