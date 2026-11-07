/**
 * Host bridge: the desktop shell injects `window.onionHost`. When the GUI runs
 * in a plain browser tab the bridge is absent and callers fall back to the
 * in-app dialogs, so nothing breaks during frontend development.
 */

export type WindowHost = {
  minimize?: () => Promise<boolean> | boolean
  toggle_maximize?: () => Promise<boolean> | boolean
  close?: () => Promise<boolean> | boolean
  is_maximized?: () => Promise<boolean> | boolean
  toggle_fullscreen?: () => Promise<boolean> | boolean
  is_fullscreen?: () => Promise<boolean> | boolean
  pick_directory?: () => Promise<string[] | null> | string[] | null
  pick_files?: () => Promise<string[] | null> | string[] | null
}

export function getHost(): WindowHost | null {
  const host = (window as unknown as { onionHost?: WindowHost }).onionHost
  return host ?? null
}

/** True when a native desktop host is available (dialogs, window controls). */
export function hasHost(): boolean {
  return getHost() !== null
}

async function callHost(
  method: 'pick_directory' | 'pick_files',
): Promise<string[] | null> {
  const host = getHost()
  const fn = host?.[method]
  if (!fn) return null
  try {
    const result = await Promise.resolve(fn.call(host))
    return Array.isArray(result) ? result : null
  } catch {
    return null
  }
}

/** Open the native folder picker. Resolves to null when unavailable/cancelled. */
export async function pickNativeDirectory(): Promise<string | null> {
  const result = await callHost('pick_directory')
  if (!result || result.length === 0) return null
  return result[0]
}

/** Open the native file picker. Resolves to null when unavailable/cancelled. */
export async function pickNativeFiles(): Promise<string[] | null> {
  const result = await callHost('pick_files')
  if (!result || result.length === 0) return null
  return result
}