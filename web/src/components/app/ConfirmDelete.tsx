import { Loader2Icon } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

export function ConfirmDelete({ open, name, pending, onCancel, onConfirm }: {
  open: boolean; name: string; pending?: boolean; onCancel: () => void; onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete this result?</DialogTitle>
          <DialogDescription>
            “{name}”: its result, its runs and the uploaded file are deleted. The layout specs and learning jobs stay. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>Cancel</Button>
          <Button variant="destructive" disabled={pending} onClick={onConfirm}>
            {pending && <Loader2Icon className="animate-spin" />} Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
