import { Link } from 'react-router'

import { PageHeader } from '@/components/app/common'

export function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" description="This address is not a page of the app." />
      <Link to="/upload" className="text-sm hover:underline">Go to Upload</Link>
    </>
  )
}
