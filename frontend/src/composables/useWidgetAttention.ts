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

import { postToHost } from '../webclient/host-bridge'

/**
 * What makes an incoming human-agent reply demand the visitor's attention.
 *
 * A reply that lands on screen while the widget is open and the tab is visible is
 * already being read, so it gets neither a sound nor a badge. Anything else — the
 * widget minimized, or the tab in the background — gets both. Bot replies and the
 * visitor's own echo never reach here: the caller decides by `type`.
 */
export function shouldAlertAgentMessage(
    type: string | undefined,
    widgetOpen: boolean,
    documentHidden: boolean
): boolean {
    if (type !== 'agent_message') return false
    return !widgetOpen || documentHidden
}

const MAX_NOTIFICATION_BODY = 120

const trimNotificationBody = (message: string): string => {
    const collapsed = message.replace(/\s+/g, ' ').trim()
    return collapsed.length > MAX_NOTIFICATION_BODY
        ? `${collapsed.slice(0, MAX_NOTIFICATION_BODY - 1)}…`
        : collapsed
}

/**
 * Ask the visitor for notification permission. Chrome drops a prompt that is not
 * tied to a user gesture, so this is only ever called from a click or a send — never
 * from page load — and the promise is deliberately not awaited.
 */
export function requestWidgetNotificationPermission(): void {
    if (typeof Notification === 'undefined') return
    if (Notification.permission !== 'default') return
    try {
        const result = Notification.requestPermission()
        if (result && typeof result.catch === 'function') result.catch(() => {})
    } catch {
        /* the browser refused because the gesture was not good enough; stay silent */
    }
}

export function showAgentNotification(agentName: string, message: string): void {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    const title = agentName.trim() || 'New reply'
    const notification = new Notification(title, {
        body: trimNotificationBody(message),
        tag: 'widget-agent-reply',
    })
    notification.onclick = () => {
        try {
            window.focus()
        } catch {
            /* the click already focused the tab */
        }
        postToHost({ type: 'WIDGET_OPEN' })
        notification.close()
    }
}

// One context per frame, created on the visitor's first interaction. Browsers
// refuse to start audio before a gesture, so a context created at page load would
// be born suspended and never play the chime.
let audioContext: AudioContext | null = null

const getAudioContext = (): AudioContext | null => {
    if (audioContext) return audioContext
    if (typeof window === 'undefined') return null
    const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctor) return null
    try {
        audioContext = new Ctor()
    } catch {
        return null
    }
    return audioContext
}

/** Create (or resume) the shared audio context from a real user gesture. */
export function primeWidgetAudio(): void {
    const context = getAudioContext()
    if (!context || context.state === 'running') return
    try {
        const resumed = context.resume()
        if (resumed && typeof resumed.catch === 'function') resumed.catch(() => {})
    } catch {
        /* still not activated — the chime stays silent until it is */
    }
}

/** A short two-tone chime, ~180 ms, synthesized — no asset, no dependency. */
export function playAlertChime(): void {
    const context = getAudioContext()
    if (!context || context.state !== 'running') return
    try {
        const start = context.currentTime
        const tone = (frequency: number, offset: number) => {
            const oscillator = context.createOscillator()
            const gain = context.createGain()
            oscillator.type = 'sine'
            oscillator.frequency.setValueAtTime(frequency, start + offset)
            gain.gain.setValueAtTime(0.0001, start + offset)
            gain.gain.exponentialRampToValueAtTime(0.12, start + offset + 0.012)
            gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.09)
            oscillator.connect(gain)
            gain.connect(context.destination)
            oscillator.start(start + offset)
            oscillator.stop(start + offset + 0.09)
        }
        tone(880, 0) // A5
        tone(1174.66, 0.09) // D6
    } catch {
        /* a browser that cannot synthesize a tone must not break the reply */
    }
}

interface IncomingAgentMessage {
    type?: string
    agent_name?: string
    message?: string
}

export interface WidgetAttentionOptions {
    /** Whether the embedder currently shows the widget. */
    isWidgetOpen: () => boolean
    /** Add one to the launcher's unread badge. */
    onUnread: () => void
    /** Clear the badge — the visitor opened the widget or came back to the tab. */
    onClear?: () => void
    isDocumentHidden?: () => boolean
    playSound?: () => void
    notify?: (agentName: string, message: string) => void
    primeAudio?: () => void
    requestPermission?: () => void
}

/**
 * Decide whether a reply deserves attention and, if it does, sound it out, badge it
 * and (when the tab is in the background) notify it.
 *
 * Every side effect is wrapped: this runs inside the socket's `chat_response`
 * handler, and a browser refusing audio or notifications must never cost the
 * visitor the message itself.
 */
export function useWidgetAttention(options: WidgetAttentionOptions) {
    const {
        isWidgetOpen,
        onUnread,
        onClear = () => {},
        isDocumentHidden = () =>
            typeof document !== 'undefined' && document.visibilityState !== 'visible',
        playSound = playAlertChime,
        notify = showAgentNotification,
        primeAudio = primeWidgetAudio,
        requestPermission = requestWidgetNotificationPermission,
    } = options

    let primed = false

    /** First real widget interaction: create the audio context and ask permission once. */
    const prime = () => {
        if (primed) return
        primed = true
        try {
            primeAudio()
        } catch (error) {
            console.error('Widget attention audio failed:', error)
        }
        try {
            requestPermission()
        } catch (error) {
            console.error('Widget attention permission failed:', error)
        }
    }

    const handleAgentMessage = (data: IncomingAgentMessage | null | undefined) => {
        if (!data) return
        const hidden = isDocumentHidden()
        if (!shouldAlertAgentMessage(data.type, isWidgetOpen(), hidden)) return

        try {
            onUnread()
        } catch (error) {
            console.error('Widget attention badge failed:', error)
        }
        try {
            playSound()
        } catch (error) {
            console.error('Widget attention sound failed:', error)
        }
        if (hidden) {
            try {
                notify(data.agent_name ?? '', data.message ?? '')
            } catch (error) {
                console.error('Widget attention notification failed:', error)
            }
        }
    }

    // Coming back to the tab with the widget open means the visitor is looking at
    // the conversation again — the badge has done its job.
    const onVisibilityChange = () => {
        if (!isWidgetOpen() || isDocumentHidden()) return
        try {
            onClear()
        } catch (error) {
            console.error('Widget attention badge reset failed:', error)
        }
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVisibilityChange)
    }

    const stop = () => {
        if (typeof document !== 'undefined') {
            document.removeEventListener('visibilitychange', onVisibilityChange)
        }
    }

    return { prime, handleAgentMessage, stop }
}
