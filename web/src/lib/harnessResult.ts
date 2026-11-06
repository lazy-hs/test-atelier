import type { HarnessTaskResult } from '@/lib/api'

type HarnessResultContext = {
  taskId: string
  projectId: number
  expectedSchema: HarnessTaskResult['result_schema']
  expectedAnalysisRunId?: string | null
  expectedSourceDocumentIds?: string[]
  authorizedSessionIds?: string[]
}

export class HarnessResultValidationError extends Error {
  constructor() {
    super('Harness 完成结果校验失败，未显示原始结果')
    this.name = 'HarnessResultValidationError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function sameStringSet(actual: unknown, expected: string[]): boolean {
  if (!Array.isArray(actual) || actual.some(value => typeof value !== 'string')) return false
  return actual.length === expected.length && new Set(actual).size === expected.length
    && actual.every(value => expected.includes(value))
}

/**
 * Validate the browser-facing envelope again before rendering model output.
 * The API is authoritative, but this prevents a stale or replayed response
 * from crossing the task/session boundary in the UI.
 */
export function validateHarnessResultEnvelope(value: unknown, context: HarnessResultContext): Record<string, unknown> {
  if (!isRecord(value)
    || value.status !== 'completed'
    || value.task_id !== context.taskId
    || value.project_id !== context.projectId
    || value.result_schema !== context.expectedSchema
    || (context.expectedAnalysisRunId !== undefined && value.analysis_run_id !== context.expectedAnalysisRunId)
    || !isRecord(value.result)
    || value.result.schema !== context.expectedSchema
    || value.result.task_id !== context.taskId
    || typeof value.result.session_id !== 'string'
    || !value.result.session_id.trim()) {
    throw new HarnessResultValidationError()
  }

  if (context.expectedSourceDocumentIds && context.expectedSourceDocumentIds.length > 0) {
    const envelopeSources = Array.isArray(value.source_document_ids) ? value.source_document_ids : []
    const envelopeMatches = sameStringSet(envelopeSources, context.expectedSourceDocumentIds)
    const resultMatches = sameStringSet(value.result.source_document_ids, context.expectedSourceDocumentIds)
    // Test-case tasks carry their source through the referenced analysis, so
    // the task envelope may intentionally leave its direct source list empty.
    if (!resultMatches || (envelopeSources.length > 0 && !envelopeMatches)) {
      throw new HarnessResultValidationError()
    }
  }

  if (context.expectedSchema === 'test-cases.v1'
    && context.expectedAnalysisRunId
    && value.result.analysis_run_id !== context.expectedAnalysisRunId) {
    throw new HarnessResultValidationError()
  }

  if (context.authorizedSessionIds && context.authorizedSessionIds.length > 0
    && !context.authorizedSessionIds.includes(value.result.session_id as string)) {
    throw new HarnessResultValidationError()
  }

  return value.result
}

