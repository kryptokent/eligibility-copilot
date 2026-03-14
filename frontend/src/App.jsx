import { useCallback, useRef, useState } from 'react'

function App() {
  const [isDragging, setIsDragging] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [eligibility, setEligibility] = useState(null)
  const [parityReport, setParityReport] = useState(null)
  const [overrideState, setOverrideState] = useState({})
  const [governanceReport, setGovernanceReport] = useState(null)
  const [isGeneratingGovernance, setIsGeneratingGovernance] = useState(false)
  const [hasTriedUpload, setHasTriedUpload] = useState(false)

  const fileInputRef = useRef(null)

  const resetStateForNewUpload = () => {
    setError('')
    setResult(null)
    setEligibility(null)
    setParityReport(null)
    setGovernanceReport(null)
    setOverrideState({})
  }

  const handleFileSelected = useCallback(async (file) => {
    if (!file) return

    // Mark that the user has attempted an upload.
    setHasTriedUpload(true)

    // Basic PDF check: MIME type and file extension.
    const isPdfMime = file.type === 'application/pdf'
    const isPdfExt = file.name.toLowerCase().endsWith('.pdf')

    if (!isPdfMime && !isPdfExt) {
      setError('Only PDF files are supported. Please upload a .pdf document.')
      return
    }

    resetStateForNewUpload()
    setIsLoading(true)

    try {
      const formData = new FormData()
      formData.append('file', file)

      const response = await fetch('http://localhost:8000/api/upload-document', {
        method: 'POST',
        body: formData,
      })

      if (!response.ok) {
        // Try to extract a clear error message from the backend.
        let message = 'The server could not process this document.'
        try {
          const data = await response.json()
          if (data?.detail) {
            message =
              typeof data.detail === 'string'
                ? data.detail
                : Array.isArray(data.detail)
                ? data.detail.map((d) => d.msg || d).join('; ')
                : JSON.stringify(data.detail)
          }
        } catch {
          // Ignore JSON parse errors and fall back to default message.
        }
        setError(`Error from document analysis service: ${message}`)
        return
      }

      const data = await response.json()
      setResult(data)

      // Immediately call the eligibility analysis endpoint with the extracted text.
      if (data?.extracted_text) {
        const analyzeResp = await fetch('http://localhost:8000/api/analyze-document', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            extracted_text: data.extracted_text,
            document_id: data.document_id,
            detected_language: data.detected_language,
          }),
        })

        if (!analyzeResp.ok) {
          let message = 'The server could not analyze eligibility for this document.'
          try {
            const errData = await analyzeResp.json()
            if (errData?.detail) {
              message =
                typeof errData.detail === 'string'
                  ? errData.detail
                  : Array.isArray(errData.detail)
                  ? errData.detail.map((d) => d.msg || d).join('; ')
                  : JSON.stringify(errData.detail)
            }
          } catch {
            // swallow JSON parse errors and keep the default message
          }
          setError(message)
        } else {
          const analyzeData = await analyzeResp.json()
          setEligibility(analyzeData)
          setParityReport(analyzeData.parity || null)
        }
      }
    } catch (err) {
      setError(
        'Unable to reach the document analysis service. Please confirm the backend is running on http://localhost:8000.',
      )
    } finally {
      setIsLoading(false)
    }
  }, [])

  const handleInputChange = (event) => {
    const file = event.target.files?.[0]
    if (file) {
      handleFileSelected(file)
    }
  }

  const handleDrop = (event) => {
    event.preventDefault()
    event.stopPropagation()
    setIsDragging(false)

    const file = event.dataTransfer.files?.[0]
    if (file) {
      handleFileSelected(file)
    }
  }

  const handleDragOver = (event) => {
    event.preventDefault()
    event.stopPropagation()
    if (!isDragging) {
      setIsDragging(true)
    }
  }

  const handleDragLeave = (event) => {
    event.preventDefault()
    event.stopPropagation()

    // Only reset when leaving the drop zone, not when moving within it.
    if (event.currentTarget.contains(event.relatedTarget)) return
    setIsDragging(false)
  }

  const handleClickUpload = () => {
    fileInputRef.current?.click()
  }

  const languageBadge =
    result?.detected_language === 'spanish'
      ? {
          label: 'Spanish detected',
          className:
            'inline-flex items-center rounded-full bg-blue-100 px-4 py-2 text-sm font-medium text-blue-800',
        }
      : result?.detected_language === 'english'
      ? {
          label: 'English detected',
          className:
            'inline-flex items-center rounded-full bg-green-100 px-4 py-2 text-sm font-medium text-green-800',
        }
      : {
          label: 'System Ready',
          className:
            'inline-flex items-center rounded-full bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700',
        }

  const handleGenerateGovernanceReport = async () => {
    if (!result?.document_id) {
      setError('Cannot generate governance report: missing document ID from analysis response.')
      return
    }

    setError('')
    setIsGeneratingGovernance(true)

    try {
      const resp = await fetch('http://localhost:8000/api/generate-governance-report', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ document_id: result.document_id }),
      })

      if (!resp.ok) {
        let message = 'The server could not generate the governance report.'
        try {
          const data = await resp.json()
          if (data?.detail) {
            message =
              typeof data.detail === 'string'
                ? data.detail
                : Array.isArray(data.detail)
                ? data.detail.map((d) => d.msg || d).join('; ')
                : JSON.stringify(data.detail)
          }
        } catch {
          // ignore JSON parse errors
        }
        setError(message)
        return
      }

      const data = await resp.json()
      setGovernanceReport(data)
    } catch (e) {
      setError('Unable to reach the governance report service on http://localhost:8000.')
    } finally {
      setIsGeneratingGovernance(false)
    }
  }

  const handleDownloadGovernanceReport = () => {
    if (!governanceReport) return

    const lines = [
      `Document ID: ${governanceReport.document_id}`,
      '',
      '=== Document Summary ===',
      governanceReport.document_summary || '',
      '',
      '=== AI Determinations ===',
      governanceReport.ai_determinations || '',
      '',
      '=== Human Overrides ===',
      governanceReport.human_overrides || '',
      '',
      '=== Language Parity Status ===',
      governanceReport.language_parity_status || '',
      '',
      '=== Audit Trail ===',
      governanceReport.audit_trail || '',
      '',
    ]

    const blob = new Blob([lines.join('\n')], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `governance-report-${governanceReport.document_id || 'document'}.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="min-h-screen bg-slate-50">
      {/* Dark navy header bar */}
      <header className="bg-[#0f1729] text-white py-4 px-6 shadow">
        <h1 className="text-2xl font-semibold">Eligibility Copilot</h1>
        <p className="text-slate-300 text-sm mt-1">AI-Assisted Benefits Intake Review</p>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-10 space-y-8">
        {/* Upload area + preview */}
        <section className="grid gap-6 md:grid-cols-2 items-start">
          {/* Upload card */}
          <div
            className={`border-2 border-dashed rounded-lg bg-white p-8 text-center cursor-pointer transition-colors ${
              isDragging
                ? 'border-blue-500 bg-blue-50'
                : 'border-slate-300 hover:border-blue-400'
            }`}
            onClick={handleClickUpload}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept="application/pdf"
              className="hidden"
              onChange={handleInputChange}
            />

            <p className="text-slate-700 font-medium">
              Drag a PDF here or <span className="text-blue-600 underline">click to upload</span>
            </p>
            <p className="mt-2 text-xs text-slate-500">
              We&apos;ll extract text using AWS Textract and detect whether the document is in
              English or Spanish.
            </p>

            {isLoading && (
              <div className="mt-6 flex flex-col items-center justify-center gap-2 text-sm text-slate-600">
                <div className="h-8 w-8 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
                <span>Processing document…</span>
              </div>
            )}
          </div>

          {/* Preview panel */}
          <div className="rounded-lg border border-slate-200 bg-white p-6 h-[320px] flex flex-col">
            <h2 className="text-sm font-semibold text-slate-700 mb-2">Extracted text preview</h2>

            {result ? (
              <>
                <div className="mb-3 text-xs text-slate-500 space-y-1">
                  <div className="font-medium text-slate-700">
                    {result.filename || 'Uploaded document'}
                  </div>
                  <div>
                    Pages:{' '}
                    <span className="font-mono">
                      {result.page_count != null ? result.page_count : 'Unknown'}
                    </span>
                  </div>
                  {result.document_id && (
                    <div>
                      Document ID:{' '}
                      <span className="font-mono text-slate-600">{result.document_id}</span>
                    </div>
                  )}
                </div>

                <div className="flex-1 overflow-auto rounded border border-slate-100 bg-slate-50 p-3 text-xs text-slate-700 whitespace-pre-wrap">
                  {result.extracted_text || 'No text was returned from OCR.'}
                </div>
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-xs text-slate-400 text-center px-4">
                Upload a PDF to see the extracted text preview here.
              </div>
            )}
          </div>
        </section>

        {/* Error + status */}
        {hasTriedUpload && error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
            {error}
          </div>
        )}

        <div className="mt-2 flex justify-center">
          <span className={languageBadge.className}>{languageBadge.label}</span>
        </div>

        {/* Eligibility checklist */}
        {eligibility?.programs?.length > 0 && (
          <section className="mt-6 rounded-lg border border-slate-200 bg-white p-6">
            <h2 className="text-base font-semibold text-slate-800 mb-4">
              Eligibility checklist (SNAP / Medicaid / CHIP)
            </h2>
            <div className="grid gap-4 md:grid-cols-3">
              {eligibility.programs.map((item) => {
                const status = item.eligibility?.toLowerCase()
                const colorClasses =
                  status === 'yes'
                    ? 'border-emerald-200 bg-emerald-50'
                    : status === 'no'
                    ? 'border-red-200 bg-red-50'
                    : 'border-amber-200 bg-amber-50'

                const badgeClasses =
                  status === 'yes'
                    ? 'bg-emerald-600 text-white'
                    : status === 'no'
                    ? 'bg-red-600 text-white'
                    : 'bg-amber-500 text-white'

                const label =
                  status === 'yes' ? 'Likely eligible' : status === 'no' ? 'Unlikely eligible' : 'Uncertain'

                const key = `${result?.document_id || 'doc'}-${item.program}`
                const state = overrideState[key] || {
                  open: false,
                  decision: '',
                  reason: '',
                  caseworkerId: '',
                  submitting: false,
                }

                const toggleOpen = (open) => {
                  setOverrideState((prev) => ({
                    ...prev,
                    [key]: {
                      ...state,
                      open,
                    },
                  }))
                }

                const updateField = (field, value) => {
                  setOverrideState((prev) => ({
                    ...prev,
                    [key]: {
                      ...state,
                      [field]: value,
                    },
                  }))
                }

                const submitOverride = async () => {
                  if (!result?.document_id) {
                    setError('Cannot log override: missing document ID from analysis response.')
                    return
                  }
                  if (!state.decision || !state.reason || !state.caseworkerId) {
                    setError('Please select a decision, provide a reason, and enter your caseworker ID.')
                    return
                  }

                  setError('')
                  setOverrideState((prev) => ({
                    ...prev,
                    [key]: {
                      ...state,
                      submitting: true,
                    },
                  }))

                  try {
                    const resp = await fetch('http://localhost:8000/api/log-override', {
                      method: 'POST',
                      headers: {
                        'Content-Type': 'application/json',
                      },
                      body: JSON.stringify({
                        document_id: result.document_id,
                        program: item.program,
                        original_determination: item.eligibility,
                        override_decision: state.decision,
                        override_reason: state.reason,
                        caseworker_id: state.caseworkerId,
                      }),
                    })

                    if (!resp.ok) {
                      let message = 'The server could not record this override.'
                      try {
                        const data = await resp.json()
                        if (data?.detail) {
                          message =
                            typeof data.detail === 'string'
                              ? data.detail
                              : Array.isArray(data.detail)
                              ? data.detail.map((d) => d.msg || d).join('; ')
                              : JSON.stringify(data.detail)
                        }
                      } catch {
                        // ignore JSON parse failures
                      }
                      setError(message)
                    } else {
                      // On success, clear and close the form for this program.
                      setOverrideState((prev) => ({
                        ...prev,
                        [key]: {
                          open: false,
                          decision: '',
                          reason: '',
                          caseworkerId: '',
                          submitting: false,
                        },
                      }))
                    }
                  } catch (e) {
                    setError('Unable to reach the override logging service on http://localhost:8000.')
                  } finally {
                    setOverrideState((prev) => ({
                      ...prev,
                      [key]: {
                        ...prev[key],
                        submitting: false,
                      },
                    }))
                  }
                }

                return (
                  <div
                    key={item.program}
                    className={`rounded-md border px-4 py-3 text-sm ${colorClasses} flex flex-col gap-2`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-800">{item.program}</span>
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${badgeClasses}`}>
                        {label}
                      </span>
                    </div>
                    <p className="text-xs text-slate-700 leading-snug">{item.reason}</p>

                    {item.missing_information?.length > 0 && (
                      <div className="mt-1">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-600">
                          Missing information
                        </div>
                        <ul className="mt-1 list-disc pl-4 text-xs text-slate-700 space-y-0.5">
                          {item.missing_information.map((m, idx) => (
                            <li key={idx}>{m}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Override controls */}
                    <button
                      type="button"
                      onClick={() => toggleOpen(!state.open)}
                      className="mt-2 inline-flex items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                    >
                      {state.open ? 'Cancel override' : 'Override'}
                    </button>

                    {state.open && (
                      <div className="mt-2 rounded-md border border-slate-200 bg-white/80 px-3 py-2 space-y-2">
                        <div className="flex gap-2">
                          <div className="flex-1">
                            <label className="block text-[11px] font-medium text-slate-600 mb-1">
                              Decision
                            </label>
                            <select
                              className="w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800"
                              value={state.decision}
                              onChange={(e) => updateField('decision', e.target.value)}
                            >
                              <option value="">Select…</option>
                              <option value="yes">Eligible</option>
                              <option value="no">Not eligible</option>
                              <option value="maybe">Uncertain / needs follow-up</option>
                            </select>
                          </div>
                          <div className="flex-1">
                            <label className="block text-[11px] font-medium text-slate-600 mb-1">
                              Caseworker ID
                            </label>
                            <input
                              type="text"
                              className="w-full rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-800"
                              value={state.caseworkerId}
                              onChange={(e) => updateField('caseworkerId', e.target.value)}
                              placeholder="e.g. jdoe-123"
                            />
                          </div>
                        </div>
                        <div>
                          <label className="block text-[11px] font-medium text-slate-600 mb-1">
                            Reason for override
                          </label>
                          <textarea
                            rows={3}
                            className="w-full rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-800"
                            value={state.reason}
                            onChange={(e) => updateField('reason', e.target.value)}
                            placeholder="Briefly explain why your decision differs from the AI suggestion."
                          />
                        </div>
                        <div className="flex justify-end">
                          <button
                            type="button"
                            onClick={submitOverride}
                            disabled={state.submitting}
                            className="inline-flex items-center justify-center rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-60"
                          >
                            {state.submitting ? 'Saving…' : 'Save override'}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {/* Language Parity Report */}
        {parityReport && (
          <section className="mt-4 rounded-lg border border-slate-200 bg-white p-6 space-y-4">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-base font-semibold text-slate-800">Language Parity Report</h2>
              <span className={languageBadge.className}>{languageBadge.label}</span>
            </div>

            <div className="flex items-center gap-3 text-sm">
              {parityReport.parity_match ? (
                <>
                  <span className="inline-flex items-center rounded-full bg-emerald-100 px-3 py-1 text-xs font-medium text-emerald-800">
                    Parity confirmed
                  </span>
                  <span className="text-slate-700">
                    English and Spanish eligibility determinations match for this document.
                  </span>
                </>
              ) : (
                <>
                  <span className="inline-flex items-center rounded-full bg-red-100 px-3 py-1 text-xs font-medium text-red-800">
                    Parity gap detected
                  </span>
                  <span className="text-slate-700">
                    At least one program has a different eligibility outcome between English and Spanish.
                  </span>
                </>
              )}
            </div>

            {!parityReport.parity_match && parityReport.differences?.length > 0 && (
              <div className="mt-2 grid gap-4 md:grid-cols-2">
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-2">
                    English determination
                  </h3>
                  <ul className="space-y-1 text-xs text-slate-800">
                    {parityReport.english_programs.map((p) => (
                      <li key={p.program}>
                        <span className="font-semibold">{p.program}:</span>{' '}
                        <span className="font-mono">{p.eligibility}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-2">
                    Spanish determination
                  </h3>
                  <ul className="space-y-1 text-xs text-slate-800">
                    {parityReport.spanish_programs.map((p) => (
                      <li key={p.program}>
                        <span className="font-semibold">{p.program}:</span>{' '}
                        <span className="font-mono">{p.eligibility}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </section>
        )}

        {/* Governance report controls + view */}
        {result?.document_id && (
          <section className="mt-4 rounded-lg border border-slate-200 bg-white p-6 space-y-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-base font-semibold text-slate-800">Governance Artifact</h2>
              <button
                type="button"
                onClick={handleGenerateGovernanceReport}
                disabled={isGeneratingGovernance}
                className="inline-flex items-center justify-center rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-60"
              >
                {isGeneratingGovernance ? 'Generating…' : 'Generate Governance Report'}
              </button>
            </div>

            {governanceReport && (
              <div className="mt-2 space-y-4">
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={handleDownloadGovernanceReport}
                    className="inline-flex items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-800 hover:bg-slate-50"
                  >
                    Download Report
                  </button>
                </div>

                <div className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-800 space-y-4">
                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-1">
                      Document Summary
                    </h3>
                    <p className="whitespace-pre-wrap text-sm">{governanceReport.document_summary}</p>
                  </div>

                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-1">
                      AI Determinations
                    </h3>
                    <p className="whitespace-pre-wrap text-sm">{governanceReport.ai_determinations}</p>
                  </div>

                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-1">
                      Human Overrides
                    </h3>
                    <p className="whitespace-pre-wrap text-sm">{governanceReport.human_overrides}</p>
                  </div>

                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-1">
                      Language Parity Status
                    </h3>
                    <p className="whitespace-pre-wrap text-sm">{governanceReport.language_parity_status}</p>
                  </div>

                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-1">
                      Audit Trail
                    </h3>
                    <p className="whitespace-pre-wrap text-sm">{governanceReport.audit_trail}</p>
                  </div>
                </div>
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  )
}

export default App
