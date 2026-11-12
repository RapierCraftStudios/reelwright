# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publishing behind approval: the queue, the YouTube adapter and chapters from beats.

Nothing reaches a platform adapter without an Approval row whose payload hash matches the
exact payload being sent (spike § Guardrails, § Security). Import the submodules directly.
"""
