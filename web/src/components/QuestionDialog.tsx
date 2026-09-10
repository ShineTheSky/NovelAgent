import { useState } from 'react';
import type { PendingQuestion, QuestionAnswer } from '../types/chat';

export function QuestionDialog({ qa }: { qa: PendingQuestion }) {
  const [answers, setAnswers] = useState<Record<number, QuestionAnswer>>({});

  const setAnswer = (index: number, value: QuestionAnswer) => {
    setAnswers(previous => ({ ...previous, [index]: value }));
  };

  const handleSubmit = () => {
    qa.onSubmit(qa.questions.map((_, index) => answers[index] ?? ''));
  };

  const allAnswered = qa.questions.every((_, index) => {
    const answer = answers[index];
    return answer !== undefined && answer !== '' && (!Array.isArray(answer) || answer.length > 0);
  });

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" role="dialog" aria-modal="true" aria-labelledby="question-dialog-title">
      <div className="bg-white rounded-2xl shadow-2xl p-6 max-w-md w-full mx-4 max-h-[80vh] overflow-y-auto">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-purple-100 flex items-center justify-center text-lg" aria-hidden="true">?</div>
          <div>
            <h3 id="question-dialog-title" className="font-semibold text-gray-800">需要确认</h3>
            <p className="text-xs text-gray-400">Agent 需要你做出选择</p>
          </div>
        </div>

        {qa.questions.map((question, index) => (
          <fieldset key={`${question.header}-${index}`} className="mb-4 last:mb-0">
            <legend className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">{question.header}</span>
              <span className="text-sm text-gray-700">{question.question}</span>
            </legend>
            <div className="space-y-1.5">
              {question.options.map(option => {
                const current = answers[index];
                const isSelected = question.multiSelect
                  ? Array.isArray(current) && current.includes(option.label)
                  : current === option.label;

                return (
                  <label key={option.label} className={`flex items-start gap-2 p-2 rounded-lg border cursor-pointer transition-colors ${isSelected ? 'border-purple-400 bg-purple-50' : 'border-gray-200 hover:bg-gray-50'}`}>
                    <input
                      type={question.multiSelect ? 'checkbox' : 'radio'}
                      name={`question-${index}`}
                      className="mt-0.5 accent-purple-600"
                      checked={isSelected}
                      onChange={() => {
                        if (!question.multiSelect) {
                          setAnswer(index, option.label);
                          return;
                        }
                        const selected = Array.isArray(current) ? [...current] : [];
                        setAnswer(index, isSelected ? selected.filter(label => label !== option.label) : [...selected, option.label]);
                      }}
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-700">{option.label}</span>
                      {option.description && <span className="block text-xs text-gray-400">{option.description}</span>}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        ))}

        <button type="button" onClick={handleSubmit} disabled={!allAnswered} className={`w-full mt-4 py-2.5 text-sm rounded-xl font-medium transition-colors ${allAnswered ? 'bg-purple-600 text-white hover:bg-purple-700' : 'bg-gray-200 text-gray-400 cursor-not-allowed'}`}>
          提交
        </button>
      </div>
    </div>
  );
}
