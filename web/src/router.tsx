import { createBrowserRouter, Navigate } from 'react-router'

import { AppShell } from '@/components/app/AppShell'
import { CostsPage } from '@/pages/CostsPage'
import { DocumentPage } from '@/pages/DocumentPage'
import { DocumentsPage } from '@/pages/DocumentsPage'
import { JobPage } from '@/pages/JobPage'
import { JobsPage } from '@/pages/JobsPage'
import { MappingPage } from '@/pages/MappingPage'
import { NotFound } from '@/pages/NotFound'
import { ReviewPage } from '@/pages/ReviewPage'
import { ReviewQueuePage } from '@/pages/ReviewQueuePage'
import { SpecPage } from '@/pages/SpecPage'
import { SpecsPage } from '@/pages/SpecsPage'
import { UploadPage } from '@/pages/UploadPage'

export const router = createBrowserRouter([
  {
    path: '/',
    Component: AppShell,
    children: [
      { index: true, element: <Navigate to="/upload" replace /> },
      { path: 'upload', Component: UploadPage },
      { path: 'documents', Component: DocumentsPage },
      { path: 'documents/:sha', Component: DocumentPage },
      { path: 'review', Component: ReviewQueuePage },
      { path: 'review/:sha', Component: ReviewPage },
      { path: 'jobs', Component: JobsPage },
      { path: 'jobs/:id', Component: JobPage },
      { path: 'costs', Component: CostsPage },
      { path: 'specs', Component: SpecsPage },
      { path: 'specs/:id', Component: SpecPage },
      { path: 'mapping', Component: MappingPage },
      { path: '*', Component: NotFound },
    ],
  },
])
