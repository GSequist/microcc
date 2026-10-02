// src/components/LoadingSpinner.tsx
import React from 'react';

export default function LoadingSpinner() {
  return (
    <div className="fixed inset-0 flex items-center justify-center backdrop-blur-sm bg-black/5 z-[10000]">
        <div className="bg-white/80 backdrop-blur-xl p-8 rounded-xl w-[90%] max-w-md shadow-lg border border-gray-200/30">
          <div className="text-base font-medium text-gray-900 mb-3 tracking-[-0.01em]">
            ⌘ We are setting up your workspace..
          </div>
          
          
          <div className="text-sm text-gray-600 text-left">
            <ul className="text-sm text-gray-600 text-left list-disc list-inside pl-2">
              <li>Please wait while we authenticate and prepare your environment...</li>
            </ul>
          </div>
        </div>
    </div>
  );
}