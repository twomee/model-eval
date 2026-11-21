import { useState } from 'react'

interface Props {
  command: string
}

export function CommandBox({ command }: Props) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle')

  async function copy() {
    try {
      await navigator.clipboard.writeText(command)
      setStatus('copied')
    } catch {
      // Clipboard needs a secure context (localhost counts) and permission: say so instead of failing silently.
      setStatus('failed')
    }
  }

  return (
    <section aria-label="Command">
      <h2>Run it</h2>
      <div className="command">
        <code>{command}</code>
        <button type="button" onClick={copy}>Copy</button>
      </div>
      <p className="muted" role="status">
        {status === 'copied' && 'Copied. Paste it in a terminal in the project folder.'}
        {status === 'failed' && 'Could not copy automatically: select the command and copy it by hand.'}
        {status === 'idle' && 'Runs resume where they stopped: if a subscription hits its limit, wait and run it again.'}
      </p>
    </section>
  )
}
