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

import { buildUploadUrl } from '../utils/avatars'

/** Class the loader styles the launcher photo with; `has-photo` on the button
 *  hides the default mark and chevron. */
export const LAUNCHER_PHOTO_CLASS = 'cm-photo'

/**
 * Resolve `customization.photo_url` for the embed loader.
 *
 * Same rule as resolveWidgetUploadUrl (widget-env.ts): relative stored paths such
 * as `/api/v1/uploads/...` are resolved against the widget API origin, and absolute
 * S3/CDN signatures and data URIs pass through untouched. `apiBase` is the loader's
 * own base URL (window.chattermateBaseUrl, APP_CONFIG, or the baked default), not
 * the widget app's — the loader runs on the customer's site and never imports env.
 */
export function resolveLauncherPhotoUrl(
  photoUrl: string | null | undefined,
  apiBase: string
): string {
  return buildUploadUrl(photoUrl, apiBase)
}

/**
 * The launcher's face when an agent photo exists, null when it does not.
 *
 * Built as a DOM node, never markup: photo_url is organization-supplied data, so
 * interpolating it into an HTML string would be an XSS sink. `alt` is empty because
 * the button is the interactive element and the image is its face, not content.
 */
export function createLauncherPhoto(src: string, doc: Document): HTMLImageElement | null {
  if (!src) return null
  const img = doc.createElement('img')
  img.className = LAUNCHER_PHOTO_CLASS
  img.alt = ''
  img.decoding = 'async'
  img.src = src
  return img
}
