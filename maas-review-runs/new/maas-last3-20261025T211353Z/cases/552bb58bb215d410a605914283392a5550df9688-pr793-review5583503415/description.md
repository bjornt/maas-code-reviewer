The test failed when the number of subnets in the db under test results in the subnet id's crossing the 100 boundary, resulting in the following error:

`AssertionError: Lists differ: ['subnet-100', 'subnet-101', 'subnet-99'] != ['subnet-99', 'subnet-100', 'subnet-101']`

This ensures that subnets are properly sorted by their id in the test.