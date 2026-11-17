"""Group consolidation entry point for the dated ownership boundary."""
from .boundary import ORG_LIMIT, parse_boundary
from .common import ident, read
from .consolidation import consolidate
from .model import analyze


def run_group(source, boundary, root):
    """Parse both documents, validate the whole timeline, then attribute."""
    root = ident(root)
    data, source_digest = read(source)
    boundary_data, boundary_digest = read(boundary)
    result = analyze(data)
    index = parse_boundary(boundary_data)
    organizations = set(index.organizations)
    organizations.update(row['organization'] for row in result['activities'])
    if len(organizations) > ORG_LIMIT:
        raise ValueError('too many organizations')
    return {
        'schema_version': 3,
        'source_sha256': source_digest,
        'boundary_sha256': boundary_digest,
        'consolidation': consolidate(result, index, root),
    }
