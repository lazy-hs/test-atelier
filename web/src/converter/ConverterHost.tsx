import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Converter from './Converter'
import styles from './converter.css?inline'
import flowStyles from '@xyflow/react/dist/style.css?inline'

export function ConverterHost({ projectId, readonly }: { projectId: number; readonly: boolean }) {
  const host = useRef<HTMLDivElement>(null)
  const [shadow, setShadow] = useState<ShadowRoot | null>(null)
  useEffect(() => {
    if (!host.current) return
    setShadow(host.current.shadowRoot ?? host.current.attachShadow({ mode: 'open' }))
  }, [])
  return <div ref={host} className="converter-host">{shadow && createPortal(<>
    <style>{flowStyles}</style>
    <style>{styles.replace(':root {', ':host {').replace('body {', ':host {')}</style>
    <Converter key={projectId} projectId={projectId} readonly={readonly} />
  </>, shadow)}</div>
}
