# How to contribute

Thanks for showing interest and sharing your time to contribute to this
project!

This guide is meant to be used as a general guideline for creating
issues or pull requests. We encourage all first-time contributors to
give this a read to avoid common mistakes and improve the quality of all
contributions.

## Opening issues

Before creating a new issue make sure you use the search functionality
to confirm that a similar issue doesn’t already exist. Next, make sure
you properly label the issue as per our
https://github.com/trento-project/web/labels[labels]

If you are reporting a `+bug+`, please share a file generated using the
`+trento-support.sh+` script with the following params:

....
# trento-support.sh --collect all --output file-tgz
....

and include the output in your issue. The script should remove sensitive
data automatically but please make sure you are not sharing any
sensitive data of your own.

## Submitting changes

Always refer to the https://github.com/trento-project/docs[docs
repository] for coding standards and general guidelines.


### Pull Requests guideline

Reviews are hard. These few points will help us to reduce the time and
effort required and allow us to merge your changes faster.

* PR for new features or structural changes must be linked to an issue.
* Use GPG signatures and Signoff in your commits. `git commit -s -S`
* Pull requests should be limited in scope to a single feature, bug fix, or
  improvement. Only touch relevant files.
* Follow the coding standards and guidelines in the docs repository.
* We have a PR template to aid you in completing the required details.
Be sure to complete it and remove the non-relevant parts.
* Keep PRs as small as possible. When the PR gets too big, consider
splitting it into multiple parts. A PR should ideally be between 100 and
400 additions. Use stacked PRs to break down large changes into smaller,
manageable parts.
* Verify your code by running tests, formatting and linters.
* Check that your code is not generating new warnings.
* Check that any dependent changes have been merged and published in
downstream modules
* Commit history should be short and group changes that otherwise
wouldn’t make sense on their own. 
* Always write a clear log message for your commits. One-line messages
are fine for small changes, but bigger changes should look like this:
* Backport changes to the release when fixing bugs and CVE.
+
[source,sh]
....
git commit -S -s -m "A brief summary of the commit

A paragraph describing what changed and its impact."
....
* Write a detailed description that gives context and explains why you
are creating the PR.
* If the PR adds functionality, please add some tests and documentation
to support it.
* Each PR needs 1 approval to be merged. Select a reviewer in particular
if you are looking for specific feedback from someone.


### AI Clause
Code or other content generated in whole or in part using AI tools can be
contributed to Trento. However, there are some unique considerations
related to AI generated content that developers should factor into their
contributions.

Contributors should ensure that the terms and conditions of the generative
AI tool do not place any contractual restrictions on how the tool’s output
can be used that are inconsistent with the project’s open source software
license, the project’s intellectual property policies, or the Open Source
Definition.

If any pre-existing copyrighted materials (including pre-existing open
source code) authored or owned by third parties are included in the AI
tool’s output, prior to contributing such output to the project, the
Contributor should confirm that they have permission from the third party
owners — such as the form of an open source license or public domain
declaration that complies with the project’s licensing policies — to use
and modify such pre-existing materials and contribute them to the project.
Additionally, the contributor should provide notice and attribution of such
third party rights, along with information about the applicable license
terms, with their contribution.

Some tools provide features that can assist contributors. For example, some
tools provide a feature that suppresses responses that are similar to third
party materials in the AI tool’s output, or a feature that flags similarity
between copyrighted training data or other materials owned by third parties
and the AI tool’s output and provides information about the licensing terms
that apply to such third party materials.


### Reviewers guideline

. Opinionated comments are welcome but don’t expect them always to be
addressed. Be ready for discussion but also open to conceding. To avoid
scope creep, consider proposing subsequent PRs rather than requesting
changes for the current PR you are reviewing.
. Short, concise comments with examples are the most valuable.
