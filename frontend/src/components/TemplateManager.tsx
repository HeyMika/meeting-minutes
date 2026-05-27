'use client';

import { useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';
import { toast } from 'sonner';
import { FolderOpen, FileUp, Info, HelpCircle } from 'lucide-react';
import { Button } from './ui/button';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';

export function TemplateManager() {
  const [isImporting, setIsProcessing] = useState(false);

  const handleOpenFolder = async () => {
    try {
      await invoke('api_open_templates_folder');
    } catch (error) {
      console.error('Failed to open templates folder:', error);
      toast.error('Failed to open templates folder');
    }
  };

  const handleImportTemplate = async () => {
    try {
      const selected = await open({
        multiple: false,
        filters: [{
          name: 'Template',
          extensions: ['json']
        }]
      });

      if (selected && !Array.isArray(selected)) {
        setIsProcessing(true);
        const result = await invoke('api_import_template', { filePath: selected });
        toast.success(`Imported template: ${(result as any).name}`);
        // Optionally refresh list if we had one here
      }
    } catch (error) {
      console.error('Failed to import template:', error);
      toast.error(typeof error === 'string' ? error : 'Failed to import template');
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <div className="mt-8 pt-8 border-t border-gray-100">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h3 className="text-lg font-semibold text-gray-900">Summary Templates</h3>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger>
                <HelpCircle className="h-4 w-4 text-gray-400" />
              </TooltipTrigger>
              <TooltipContent className="max-w-xs p-3">
                <p className="text-xs leading-relaxed">
                  Templates define the structure of your AI summaries. 
                  Custom templates added here will persist even after application updates.
                </p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      <div className="bg-blue-50 border border-blue-100 rounded-lg p-4 mb-6 flex gap-3">
        <Info className="h-5 w-5 text-blue-500 shrink-0 mt-0.5" />
        <div className="text-sm text-blue-800">
          <p className="font-medium mb-1">Make your templates permanent</p>
          <p className="text-blue-700/80">
            By importing templates or adding them to the folder below, they are stored in your 
            system's Application Support directory, making them permanent across all future app versions.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        <Button 
          variant="outline" 
          onClick={handleOpenFolder}
          className="flex items-center gap-2"
        >
          <FolderOpen className="h-4 w-4" />
          Open Templates Folder
        </Button>

        <Button 
          variant="default" 
          onClick={handleImportTemplate}
          disabled={isImporting}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700"
        >
          <FileUp className="h-4 w-4" />
          {isImporting ? 'Importing...' : 'Import New Template'}
        </Button>
      </div>

      <p className="mt-4 text-xs text-gray-500 italic">
        Supported format: JSON files matching the Meetily Template schema.
      </p>
    </div>
  );
}
