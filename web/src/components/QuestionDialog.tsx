import { useState } from 'react';

interface Question {
  question: string;
  header: string;
  options: { label: string; description: string }[];
  multiSelect: boolean;
}

interface PendingQuestion {
  questions: Question[];
  onSubmit: (answers: Record<string, string | string[]>) => void;
}

export function QuestionDialog({ qa }: { qa: PendingQuestion }) {
  const [answers, setAnswers] = useState<Record<string, (string | string[])>>({});

  const setAnswer = (idx: number, value: string | string[]) => {
    setAnswers(prev => ({ ...prev, [idx]: value }));
  };

  const handleSubmit = () => {
    // Fill empty answers with empty string
    const result = qa.questions.map((_, i) => answers[i] ?? '');
    qa.onSubmit(result);
  };

  const allAnswered = qa.questions.every((_, i) => {
    const a = answers[i];
    return a !== undefined && a !== '' && (Array.isArray(a) ? a.length > 0 : true);
  });

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-white rounded-2xl shadow-2xl p-6 max-w-md w-full mx-4 max-h-[80vh] overflow-y-auto">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-purple-100 flex items-center justify-center text-lg" aria-hidden="true">?</div>
          <div>
            <h3 className="font-semibold text-gray-800">需要确认</h3>
            <p className="text-xs text-gray-400">Agent 需要你做出选择</p>
          </div>
        </div>

        {qa.questions.map((q, idx) => (
          <div key={idx} className="mb-4 last:mb-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">{q.header}</span>
              <span className="text-sm text-gray-700">{q.question}</span>
            </div>
            <div className="space-y-1.5">
              {q.options.map((opt, oi) => {
                const current = answers[idx];
                const isSelected = q.multiSelect
                  ? Array.isArray(current) && current.includes(opt.label)
                  : current === opt.label;

                return (
                  <label
                    key={oi}
                    className={`flex items-start gap-2 p-2 rounded-lg border cursor-pointer transition-colors ${
                      isSelected ? 'border-purple-400 bg-purple-50' : 'border-gray-200 hover:bg-gray-50'
                    }`}
                  >
                    <input
                      type={q.multiSelect ? 'checkbox' : 'radio'}
                      name={`q-${idx}`}
                      className="mt-0.5 accent-purple-600"
                      checked={isSelected}
                      onChange={() => {
                        if (q.multiSelect) {
                          const cur = Array.isArray(current) ? [...current] : [];
                          if (isSelected) {
                            setAnswer(idx, cur.filter(l => l !== opt.label));
                          } else {
                            setAnswer(idx, [...cur, opt.label]);
                          }
                        } else {
                          setAnswer(idx, opt.label);
                        }
                      }}
                    />
                    <div>
                      <div className="text-sm font-medium text-gray-700">{opt.label}</div>
                      {opt.description && <div className="text-xs text-gray-400">{opt.description}</div>}
                    </div>
                  </label>
                );
              })}
            </div>
          </div>
        ))}

        <button
          onClick={handleSubmit}
          disabled={!allAnswered}
          className={`w-full mt-4 py-2.5 text-sm rounded-xl font-medium transition-colors ${
            allAnswered ? 'bg-purple-600 text-white hover:bg-purple-700' : 'bg-gray-200 text-gray-400 cursor-not-allowed'
          }`}
        >
          提交
        </button>
      </div>
    </div>
  );
}
